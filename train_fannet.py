import torch
import torch.nn as nn
from torch.optim import Adam
from torch.utils.data import DataLoader
from torch.cuda.amp import GradScaler
import argparse
from pathlib import Path
import time
import math
from tqdm import tqdm

from utils import get_config, set_seed, get_elapsed_time, save_model, ROOT
from data import FANnetDataset
from models.fannet import FANnet


def get_args():
    parser = argparse.ArgumentParser()

    # parser.add_argument("--run_id", type=str, required=False)
    # parser.add_argument("--fannet_dir", type=str, required=True)
    parser.add_argument("--n_epochs", type=int, required=True)
    parser.add_argument("--batch_size", type=int, required=True)
    # parser.add_argument("--lr", type=float, required=True)
    parser.add_argument("--n_cpus", type=int, required=False, default=0)

    parser.add_argument("--torch_compile", action="store_true", required=False)

    args = parser.parse_args()
    return args


def train_single_step(src_image, trg_image, one_hot, fannet, optim, scaler, crit, device):
    src_image = src_image.to(device)
    trg_image = trg_image.to(device)
    one_hot = one_hot.to(device)

    with torch.autocast(
        device_type=device.type,
        dtype=torch.float16 if device.type == "cuda" else torch.bfloat16,
        enabled=True if device.type == "cuda" else False,
    ):
        pred = fannet(src_image, one_hot)
        loss = crit(pred, trg_image)
    optim.zero_grad()
    if CONFIG["DEVICE"].type == "cuda":
        scaler.scale(loss).backward()
        scaler.step(optim)
        scaler.update()
    else:
        loss.backward()
        optim.step()
    return loss.item()


@torch.no_grad()
def validate(val_dl, fannet, crit, device):
    fannet.eval()

    cum_loss = 0
    for src_image, trg_image, one_hot in tqdm(val_dl, desc=f"Validating...", leave=False):
        src_image = src_image.to(device)
        trg_image = trg_image.to(device)
        one_hot = one_hot.to(device)

        pred = fannet(src_image, one_hot)
        loss = crit(pred, trg_image)
        cum_loss += loss
    val_loss = cum_loss / len(val_dl)

    fannet.train()
    return val_loss


if __name__ == "__main__":
    args = get_args()
    CONFIG = get_config(
        config_path=ROOT/"configs/fannet.yaml", args=args,
    )

    FANNET_DIR = ROOT/"dataset/fannet"
    train_ds = FANnetDataset(fannet_dir=FANNET_DIR, split="train")
    train_dl = DataLoader(
        train_ds,
        batch_size=CONFIG["BATCH_SIZE"],
        shuffle=True,
        num_workers=CONFIG["N_CPUS"],
        pin_memory=True,
        drop_last=True,
    )
    val_ds = FANnetDataset(fannet_dir=FANNET_DIR, split="val")
    val_dl = DataLoader(
        val_ds,
        batch_size=CONFIG["BATCH_SIZE"],
        shuffle=False,
        num_workers=CONFIG["N_CPUS"],
        pin_memory=True,
        drop_last=True,
    )

    fannet = FANnet(dim=CONFIG["ARCHITECTURE"]["DIM"]).to(CONFIG["DEVICE"])
    if CONFIG["TORCH_COMPILE"]:
        fannet = torch.compile(fannet)

    # "The network minimizes the mean absolute error (MAE)."
    crit = nn.L1Loss(reduction="mean")

    lr = CONFIG["ADAM"]["LR"] * (CONFIG["BATCH_SIZE"] ** 0.5)
    optim = Adam(
        fannet.parameters(),
        lr=CONFIG["ADAM"]["LR"],
        betas=(CONFIG["ADAM"]["BETA1"], CONFIG["ADAM"]["BETA2"]),
        eps=CONFIG["ADAM"]["EPS"],
    )

    scaler = GradScaler(enabled=True if CONFIG["DEVICE"].type == "cuda" else False)

    min_val_loss = math.inf
    prev_save_path = Path(".pth")
    for epoch in range(1, CONFIG["TRAIN"]["N_EPOCHS"] + 1):
        cum_loss = 0
        start_time = time.time()
        for src_image, trg_image, one_hot in tqdm(train_dl, desc=f"Epoch {epoch}", leave=False):
            loss = train_single_step(
                src_image=src_image,
                trg_image=trg_image,
                one_hot=one_hot,
                fannet=fannet,
                optim=optim,
                scaler=scaler,
                crit=crit,
                device=CONFIG["DEVICE"],
            )
            cum_loss += loss
        train_loss = cum_loss / len(train_dl)

        val_loss = validate(val_dl=val_dl, fannet=fannet, crit=crit, device=CONFIG["DEVICE"])
        if val_loss < min_val_loss:
            min_val_loss = val_loss

            cur_save_path = CONFIG["CKPTS_DIR"]/f"fannet_epoch_{epoch}.pth"
            save_model(model=fannet, save_path=cur_save_path)
            if prev_save_path.exists():
                prev_save_path.unlink()
            prev_save_path = cur_save_path

        msg = f"[ {get_elapsed_time(start_time)} ]"
        msg += f"""[ {epoch}/{CONFIG["N_EPOCHS"]} ]"""
        msg += f"[ Train loss: {train_loss:.4f} ]"
        msg += f"[ Validation loss: {val_loss:.4f} ]"
        msg += f"[ Min validation loss: {min_val_loss:.4f} ]"
        print(msg)
