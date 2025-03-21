import numpy as np
import torch
import torch.nn as nn
from typing import List
import torch.nn.functional as F
import sys, os
import os.path as osp
sys.path.append(osp.dirname(osp.dirname(__file__)))

from utils.weight_init import weight_init

def autopad(k, p=None):  # kernel, padding
    # Pad to 'same'
    if p is None:
        p = k // 2 if isinstance(k, int) else [x // 2 for x in k]  # auto-pad
    return p

def u3pblock(in_ch, out_ch, num_block=2, kernel_size=3, padding=1, down_sample=False):
    m = []
    if down_sample:
        m.append(nn.MaxPool2d(kernel_size=2))
    for _ in range(num_block):
        m += [nn.Conv2d(in_ch, out_ch, kernel_size, bias=False, padding=padding),
              nn.BatchNorm2d(out_ch),
              nn.ReLU(inplace=True)]
        in_ch = out_ch
    return nn.Sequential(*m)

def en2dec_layer(in_ch, out_ch, scale):
    m = [nn.Identity()] if scale == 1 else [nn.MaxPool2d(scale, scale, ceil_mode=True)]
    m.append(u3pblock(in_ch, out_ch, num_block=1))
    return nn.Sequential(*m)

def dec2dec_layer(in_ch, out_ch, scale, fast_up=True):
    up = [nn.Upsample(scale_factor=scale, mode='bilinear', align_corners=True) if scale != 1 else nn.Identity()]
    m = [u3pblock(in_ch, out_ch, num_block=1)]
    if fast_up:
        m = m + up
    else:
        m = up + m  # used in paper
    return nn.Sequential(*m)


class FullScaleSkipConnect(nn.Module):
    def __init__(self,
                 en_channels,   # encoder out channels, high to low
                 en_scales,
                 num_dec,       # number of decoder out
                 skip_ch=64,
                 dec_scales=None,
                 bottom_dec_ch=1024,
                 dropout=0.3,
                 fast_up=True,):

        super().__init__()
        concat_ch = skip_ch * (len(en_channels) + num_dec)

        # encoder maps to decoder maps connections
        self.en2dec_layers = nn.ModuleList()
        # print(en_scales)
        for ch, scale in zip(en_channels, en_scales):
            self.en2dec_layers.append(en2dec_layer(ch, skip_ch, scale))

        # decoder maps to decoder maps connections
        self.dec2dec_layers = nn.ModuleList()
        if dec_scales is None:
            dec_scales = []
            for ii in reversed(range(num_dec)):
                dec_scales.append(2 ** (ii + 1))
        for ii, scale in enumerate(dec_scales):
            dec_ch = bottom_dec_ch if ii == 0 else concat_ch
            self.dec2dec_layers.append(dec2dec_layer(dec_ch, skip_ch, scale, fast_up=fast_up))

        self.droupout = nn.Dropout(dropout)
        self.fuse_layer = u3pblock(concat_ch, concat_ch, 1)

    def forward(self, en_maps, dec_maps=None):
        out = []
        for en_map, layer in zip(en_maps, self.en2dec_layers):
            out.append(layer(en_map))
        if dec_maps is not None and len(dec_maps) > 0:
            for dec_map, layer in zip(dec_maps, self.dec2dec_layers):
                out.append(layer(dec_map))
        return self.fuse_layer(self.droupout(torch.cat(out, 1)))


class U3PEfficientNetEncoder(nn.Module):
    def __init__(self, channels=[3, 64, 128, 256, 512, 1024], num_block=2, backbone='efficientnet-b5', pretrained=False):
        super().__init__()

        # Step 1. Select the encoder
        if backbone == 'efficientnet-b0':
            efficientnet = efficientnet_b0(pretrained=pretrained)
            cfg = efficientnet_cfg['efficientnet-b0']
        elif backbone == 'efficientnet-b5':
            efficientnet = efficientnet_b5(pretrained=pretrained)
            cfg = efficientnet_cfg['efficientnet-b5']
        elif backbone == 'efficientnet-b6':
            efficientnet = efficientnet_b6(pretrained=pretrained)
            cfg = efficientnet_cfg['efficientnet-b6']
        else:
            raise ValueError(f'Unsupported backbone : {backbone}')

        # Step 2. Check if pretrained
        if not pretrained:
            efficientnet.apply(weight_init)
        self.backbone = create_feature_extractor(efficientnet, return_nodes=efficientnet_cfg['return_nodes'])

        # Step 3. Layer to compress features to match channel sizes
        self.compress_convs = nn.ModuleList()
        for fe_ch, ch in zip(cfg['fe_channels'], cfg['channels']):
            if fe_ch != ch:
                self.compress_convs.append(nn.Conv2d(fe_ch, ch, 1, bias=False))
            else:
                self.compress_convs.append(nn.Identity())
        self.channels = channels  # Keep the original channels format for compatibility

    def forward(self, x):
        # Get backbone features
        out = self.backbone(x)

        # Compress features to match channel sizes
        encoder_out = []
        for ii, compress in enumerate(self.compress_convs):
            compressed_feature = compress(out[f'layer{ii}'])
            encoder_out.append(compressed_feature)

        return encoder_out


class U3PEfficientNetDecoder(nn.Module):
    def __init__(self, en_channels=[64, 128, 256, 512, 1024], skip_ch=64, dropout=0.3, fast_up=True):
        super().__init__()

        # Keep the same initialization format as U3PDecoder
        self.decoders = nn.ModuleDict()
        en_channels = en_channels[::-1]
        num_en_ch = len(en_channels)

        for ii in range(num_en_ch):
            if ii == 0:
                # First decoding output is identity mapping of last encoder map
                self.decoders['decoder1'] = nn.Identity()
                continue

            self.decoders[f'decoder{ii+1}'] = FullScaleSkipConnect(
                en_channels[ii:],
                en_scales=2 ** np.arange(0, num_en_ch-ii),
                num_dec=ii,
                skip_ch=skip_ch,
                bottom_dec_ch=en_channels[0],
                dropout=dropout,
                fast_up=fast_up
            )

    def forward(self, enc_map_list: List[torch.Tensor]):
        dec_map_list = []
        enc_map_list = enc_map_list[::-1]

        for ii, layer_key in enumerate(self.decoders):
            layer = self.decoders[layer_key]
            if ii == 0:
                dec_map_list.append(layer(enc_map_list[0]))
                continue
            dec_map_list.append(layer(enc_map_list[ii:], dec_map_list))

        return dec_map_list


class UNet3Plus(nn.Module):

    def __init__(self,
                 num_classes=1,
                 skip_ch=64,
                 aux_losses=2,
                 encoder: U3PEfficientNetEncoder = None,
                 channels=[3, 64, 128, 256, 512, 1024],
                 dropout=0.3,
                 transpose_final=False,
                 use_cgm=True,
                 fast_up=True):
        super().__init__()

        self.encoder = U3PEfficientNetEncoder(channels) if encoder is None else encoder
        channels = self.encoder.channels
        num_decoders = len(channels) - 1
        decoder_ch = skip_ch * num_decoders

        self.decoder = U3PEfficientNetDecoder(self.encoder.channels[1:], skip_ch=skip_ch, dropout=dropout, fast_up=fast_up)
        self.decoder.apply(weight_init)

        self.cls = nn.Sequential(
                    nn.Dropout(p=0.5),
                    nn.Conv2d(channels[-1], 2, 1),
                    nn.AdaptiveMaxPool2d(1),
                    nn.Sigmoid()
                ) if use_cgm else None

        if transpose_final:
            self.head = nn.Sequential(
                nn.ConvTranspose2d(decoder_ch, num_classes, kernel_size=4, stride = 2, padding=1, bias=False),
            )
        else:
            self.head = nn.Conv2d(decoder_ch, num_classes, 3, padding=1)
        self.head.apply(weight_init)

        if aux_losses > 0:
            self.aux_head = nn.ModuleDict()
            layer_indices = np.arange(num_decoders - aux_losses - 1, num_decoders - 1)
            for ii in layer_indices:
                ch = decoder_ch if ii != 0 else channels[-1]
                self.aux_head.add_module(f'aux_head{ii}', nn.Conv2d(ch, num_classes, 3, padding=1))
            self.aux_head.apply(weight_init)
        else:
            self.aux_head = None

    def forward(self, x):
        _, _, h, w = x.shape
        de_out = self.decoder(self.encoder(x))
        have_obj = 1

        pred = self.resize(self.head(de_out[-1]), h, w)

        pred = {'out': pred}
        if self.aux_head is not None:
            for ii, de in enumerate(de_out[:-1]):
                if ii == 0:
                    if self.cls is not None:
                        pred['cls'] = self.cls(de).squeeze(3).squeeze(2)  # (B,N,1,1)->(B,N)
                        have_obj = torch.argmax(pred['cls'], dim=1)
                head_key = f'aux_head{ii}'
                if head_key in self.aux_head:
                    de: torch.Tensor = de * have_obj
                    # de = self.dotProduct(de,have_obj)
                    pred[f'aux{ii}'] = self.resize(self.aux_head[head_key](de), h, w)

        return pred

    def dotProduct(self, seg, cls):
        B, N, H, W = seg.size()
        seg = seg.view(B, N, H * W)
        final = torch.einsum("ijk,ij->ijk", [seg, cls])
        final = final.view(B, N, H, W)
        return final

    def resize(self, x, h, w) -> torch.Tensor:
        _, _, xh, xw = x.shape
        if xh != h or xw != w:
            x = F.interpolate(x, size=(h, w), mode='bilinear', align_corners=True)
        return x


if __name__ == '__main__':
    input = torch.randn((2, 3, 320, 320))
    model = UNet3Plus(num_classes=7)
    out = model(input)
