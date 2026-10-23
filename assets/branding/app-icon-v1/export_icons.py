"""从已确认的图标母版（`source/approved-original.png`）批量导出多尺寸 PNG 与 Windows ICO 资源。

设计原则：
1. 严格保留已确认母版的构图、色彩与透明通道，不做重新设计、裁剪或背景修改；
2. 缩放前先转换至预乘 Alpha（`RGBa`）色彩空间，使用 Lanczos 滤波器重采样后再还原为 `RGBA`，
   消除透明边缘在深色/浅色任务栏下的黑边伪影；
3. 自动生成浅色与深色背景对照预览图（`preview-light-dark.png`）、SHA-256 资产清单（`manifest.json`）及打包归档；
4. 仅在重新导出图标素材时需要 `Pillow` 库，应用运行态与常规构建不依赖 `Pillow`。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256, 512, 1024)
ICO_SIZES = tuple(icon_size for icon_size in SIZES if icon_size <= 256)


def main() -> None:
    """导出各尺寸 PNG、Windows 多帧 ICO、浅/深色背景预览图与 SHA-256 清单，并逐帧校验像素一致性。"""
    original_path = ROOT / "source" / "approved-original.png"
    master_image = Image.open(original_path).convert("RGBA")
    assert master_image.width == master_image.height

    rendered_frames: dict[int, Image.Image] = {}
    (ROOT / "png").mkdir(exist_ok=True)
    (ROOT / "windows").mkdir(exist_ok=True)

    for target_size in SIZES:
        # 先转换为预乘透明度（RGBa）再执行 Lanczos 缩放，避免透明像素边缘混入黑色背景
        resized_frame = (
            master_image.convert("RGBa")
            .resize((target_size, target_size), Image.Resampling.LANCZOS)
            .convert("RGBA")
        )
        resized_frame.save(ROOT / "png" / f"obs-tool-{target_size}.png")
        rendered_frames[target_size] = resized_frame

    # 生成包含 16~256 px 全部标准尺寸帧的 Windows ICO 文件
    ico_path = ROOT / "windows" / "obs-tool.ico"
    rendered_frames[256].save(
        ico_path,
        format="ICO",
        sizes=[(frame_size, frame_size) for frame_size in ICO_SIZES],
        append_images=[
            rendered_frames[frame_size]
            for frame_size in ICO_SIZES
            if frame_size != 256
        ],
    )

    # 回读验证 ICO 内嵌尺寸集合与每一帧的 RGBA 原始像素字节完全一致
    loaded_ico = Image.open(ico_path)
    assert loaded_ico.ico.sizes() == {(frame_size, frame_size) for frame_size in ICO_SIZES}
    for frame_size in ICO_SIZES:
        assert (
            loaded_ico.ico.getimage((frame_size, frame_size)).convert("RGBA").tobytes()
            == rendered_frames[frame_size].tobytes()
        )

    # 验证导出的全部单张 PNG 尺寸、RGBA 模式及四角透明通道完整
    for target_size in SIZES:
        png_check = Image.open(ROOT / "png" / f"obs-tool-{target_size}.png")
        assert png_check.size == (target_size, target_size) and png_check.mode == "RGBA"
        assert png_check.getpixel((0, 0))[3] == 0

    # 绘制浅色（#F5F6F8）与深色（#202631）双背景视觉对照图
    preview_sheet = Image.new("RGB", (1120, 740), "white")
    sheet_draw = ImageDraw.Draw(preview_sheet)
    label_font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 18)
    small_preview_sizes = (16, 20, 24, 32, 40, 48, 64, 96, 128)

    for band_index, background_hex in enumerate(("#F5F6F8", "#202631")):
        band_top_y = band_index * 370
        sheet_draw.rectangle(
            (0, band_top_y, 1120, band_top_y + 369), fill=background_hex
        )
        text_color = "#182230" if band_index == 0 else "#EDF3FA"
        sheet_draw.text(
            (24, band_top_y + 18),
            (
                "OBS icon v1 | light background"
                if band_index == 0
                else "OBS icon v1 | dark background"
            ),
            font=label_font,
            fill=text_color,
        )
        preview_sheet.paste(
            rendered_frames[256], (20, band_top_y + 72), rendered_frames[256]
        )
        sheet_draw.text((104, band_top_y + 330), "256 px", font=label_font, fill=text_color)

        for grid_idx, icon_size in enumerate(small_preview_sizes):
            cell_left_x = 310 + (grid_idx % 5) * 158
            cell_top_y = band_top_y + 72 + (grid_idx // 5) * 150
            preview_sheet.paste(
                rendered_frames[icon_size],
                (
                    cell_left_x + (132 - icon_size) // 2,
                    cell_top_y + (128 - icon_size) // 2,
                ),
                rendered_frames[icon_size],
            )
            sheet_draw.text(
                (cell_left_x + 35, cell_top_y + 125),
                f"{icon_size} px",
                font=label_font,
                fill=text_color,
            )

    preview_sheet.save(ROOT / "preview-light-dark.png")

    # 生成包含所有导出文件 SHA-256 摘要的 manifest.json
    manifest: dict[str, object] = dict(
        version=1,
        source="source/approved-original.png",
        source_size=list(master_image.size),
        treatment="Approved composition preserved; resize and format conversion only; no redraw or cleanup.",
        png_sizes=list(SIZES),
        ico_sizes=list(ICO_SIZES),
        validation={
            "png_dimensions_and_alpha": True,
            "ico_frames_match_png": True,
            "application_integration": False,
            "exe_rebuilt": False,
        },
        files={},
    )
    file_hashes: dict[str, str] = {}
    for file_path in sorted(ROOT.rglob("*")):
        if file_path.is_file() and file_path.name != "manifest.json":
            file_hashes[file_path.relative_to(ROOT).as_posix()] = hashlib.sha256(
                file_path.read_bytes()
            ).hexdigest()
    manifest["files"] = file_hashes
    (ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 打包并逐文件校验 ZIP 归档
    archive_path = ROOT.parent / "obs-tool-icon-pack-v1.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zip_bundle:
        for file_path in sorted(ROOT.rglob("*")):
            if file_path.is_file():
                zip_bundle.write(
                    file_path, "app-icon-v1/" + file_path.relative_to(ROOT).as_posix()
                )
    with zipfile.ZipFile(archive_path) as zip_bundle:
        assert zip_bundle.testzip() is None
        for member_name in zip_bundle.namelist():
            local_file = ROOT / member_name.removeprefix("app-icon-v1/")
            assert zip_bundle.read(member_name) == local_file.read_bytes()

    print(
        json.dumps(
            {
                "archive": str(archive_path),
                "png_count": len(SIZES),
                "ico_frames": len(ICO_SIZES),
                "verified": True,
            }
        )
    )


if __name__ == "__main__":
    main()
