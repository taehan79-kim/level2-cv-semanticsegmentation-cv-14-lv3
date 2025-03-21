import os
import sys
import time
import random
import numpy as np
import torch
from tqdm import tqdm
import torch.nn.functional as F
import wandb

# Default paths
from dataset import CLASSES
from functions import dice_coef
import matplotlib.pyplot as plt
from tools.streamlit.visualize import visualize_prediction


def convert_seconds_to_hms(seconds):
    """초를 시, 분, 초로 변환하는 함수"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}"


def train(model, data_loader, val_loader, criterion, optimizer, num_epochs, val_interval, save_dir, hook=None):
    print('Start training..')

    best_dice = 0.
    total_start_time = time.time()  # 총 학습 시작 시간 추가
    best_model_path = None  # 최적 모델 파일 경로를 추적하기 위한 변수

    for epoch in range(num_epochs):
        epoch_start_time = time.time()  # 에포크 시작 시간

        model.train()

        epoch_loss = 0
        class_losses = torch.zeros(len(CLASSES)).cuda()

        with tqdm(total=len(data_loader), desc=f"Epoch [{epoch+1}/{num_epochs}]") as pbar:
            for step, (images, masks) in enumerate(data_loader):
                images, masks = images.cuda(), masks.cuda()
                model = model.cuda()

                outputs = model(images)['out']
                # outputs = model(images)
                loss = criterion(outputs, masks)

                # 클래스별 손실 계산
                for c in range(len(CLASSES)):
                    class_losses[c] += criterion(outputs[:, c:c+1], masks[:, c:c+1]).item()

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                epoch_loss += loss.item()
                pbar.set_postfix(loss=round(loss.item(), 4))
                pbar.update(1)

         # 에폭별 평균 계산
        epoch_loss = epoch_loss / len(data_loader)
        class_losses = class_losses / len(data_loader)

        # 에포크 시간 계산
        epoch_time = time.time() - epoch_start_time
        remaining_epochs = num_epochs - (epoch + 1)
        estimated_remaining_time = epoch_time * remaining_epochs

        # 시, 분, 초로 변환
        epoch_str = convert_seconds_to_hms(epoch_time)
        remaining_str = convert_seconds_to_hms(estimated_remaining_time)

        print(
            f'Epoch {epoch+1} completed in {epoch_str}. '
            f'Estimated remaining time: {remaining_str}.'
        )

        # Wandb 로깅 - train metrics
        metrics = {
            "total/train_loss": epoch_loss, # 총 train loss
            **{f"train_loss_per_class/{c}": loss.item()
               for c, loss in zip(CLASSES, class_losses)}, # 클래스별 train loss
            "epoch": epoch + 1
        }

        # Optional hook call after training epoch
        if hook is not None:
            class DummyRunner:
                def __init__(self, model, log_buffer):
                    self.model = model
                    self.log_buffer = log_buffer

            log_buffer = {'val_dice': best_dice}
            runner = DummyRunner(model, log_buffer)
            hook.after_train_epoch(runner)

        # Validation 수행
        if (epoch + 1) % val_interval == 0:
            dice, val_loss, class_val_losses, worst_samples, dices_per_class = validation(
                epoch + 1, model, val_loader, criterion)

            # scheduler
            scheduler.step()

            # Learning rate 로깅 추가
            current_lr = optimizer.param_groups[0]['lr']
            metrics["learning_rate"] = current_lr

            # Wandb 로깅 - validation metrics
            metrics.update({
                "total/val_loss": val_loss, # 총 validation loss
                "total/val_dice": dice, # 총 validation dice
                **{f"val_loss_per_class/{c}": loss
                   for c, loss in zip(CLASSES, class_val_losses)}, # 클래스별 validation loss
                **{f"val_dice_per_class/{c}": d.item()
                   for c, d in zip(CLASSES, dices_per_class)} # 클래스별 validation dice
            })

            # Worst samples 시각화
            for idx, (img, pred, true_mask, dice_score) in enumerate(worst_samples):
                fig = visualize_prediction(img, pred, true_mask)
                metrics[f"worst_sample_{idx+1}"] = wandb.Image(fig,
                    caption=f"Dice Score: {dice_score:.4f}")
                plt.close(fig)

            # Best model 저장_wandb의 Artifacts 탭에서 확인 가능
            if best_dice < dice:
                print(f"Best performance at epoch: {epoch + 1}, {best_dice:.4f} -> {dice:.4f}")
                best_dice = dice

                if best_model_path and os.path.exists(best_model_path):
                    os.remove(best_model_path)

                best_model_path = os.path.join(save_dir, f"best_dice_{best_dice:.4f}.pt")
                torch.save(model.state_dict(), best_model_path)

                wandb.run.summary.update({
                    "best_dice": best_dice,
                    "best_epoch": epoch + 1,
                    "best_model_path": best_model_path
                })

        # wandb 로깅
        wandb.log(metrics)

    total_time = time.time() - total_start_time
    total_str = convert_seconds_to_hms(total_time)
    print(f'Total training completed in {total_str}.')

    return best_dice


def validation(epoch, model, data_loader, criterion, thr=0.5, num_worst_samples=4):
    print(f'Start validation #{epoch:2d}')
    model.eval()

    dices = []
    samples = []
    total_loss = 0
    class_losses = torch.zeros(len(CLASSES)).cuda()

    with torch.no_grad():
        for step, (images, masks) in tqdm(enumerate(data_loader), total=len(data_loader)):
            images, masks = images.cuda(), masks.cuda()
            outputs = model(images)['out']
            # outputs = model(images)

            output_h, output_w = outputs.size(-2), outputs.size(-1)
            mask_h, mask_w = masks.size(-2), masks.size(-1)

            # gt와 prediction의 크기가 다른 경우 prediction을 gt에 맞춰 interpolation 합니다.
            if output_h != mask_h or output_w != mask_w:
                outputs = F.interpolate(outputs, size=(mask_h, mask_w), mode="bilinear")

            # 전체 손실 계산
            loss = criterion(outputs, masks)
            total_loss += loss.item()

            # 클래스별 손실 계산
            for c in range(len(CLASSES)):
                class_losses[c] += criterion(outputs[:, c:c+1], masks[:, c:c+1]).item()

            outputs = torch.sigmoid(outputs)
            outputs = (outputs > thr).detach().cpu()
            masks = masks.detach().cpu()

            # 배치 내 각 이미지에 대한 Dice score 계산
            batch_dices = dice_coef(outputs, masks)
            dices.append(batch_dices)

            # worst samples 수집
            for i in range(len(images)):
                sample_dice = batch_dices[i].mean().item()
                samples.append((
                    images[i].cpu().numpy().transpose(1,2,0),
                    outputs[i].numpy(),
                    masks[i].cpu().numpy(),
                    sample_dice
                ))

    dices = torch.cat(dices, 0)
    dices_per_class = torch.mean(dices, 0)

    dice_str = [
        f"{c:<12}: {d.item():.4f}"
        for c, d in zip(CLASSES, dices_per_class)
    ]
    dice_str = "\n".join(dice_str)
    print(dice_str)

    avg_loss = total_loss / len(data_loader)
    class_losses = class_losses / len(data_loader)
    avg_dice = torch.mean(dices_per_class).item()

    # worst samples 정렬
    worst_samples = sorted(samples, key=lambda x: x[3])[:num_worst_samples]

    return avg_dice, avg_loss, class_losses.cpu().numpy(), worst_samples, dices_per_class


def set_seed(seed=123):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(seed)
    random.seed(seed)
