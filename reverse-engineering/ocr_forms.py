"""OCR the form panels captured from real PCLink: title, then each row's caption."""
import json, subprocess, sys, tempfile
from pathlib import Path
from PIL import Image, ImageOps

ROW_TOP, ROW_H, ROWS = 24, 21, 16  # Panel rows, relative to the crop.
CAPTION_X = (28, 368)


def text(image, psm="7"):
    with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
        ImageOps.invert(image.convert("L")).resize((image.width * 3, image.height * 3), Image.LANCZOS).save(tmp.name)
        return subprocess.run(["tesseract", tmp.name, "-", "--psm", psm], capture_output=True, text=True).stdout.strip()


def read(path):
    im = Image.open(path).convert("RGB")
    title = text(im.crop((60, 0, 560, 22)))
    rows = []
    for row in range(ROWS):
        y = ROW_TOP + row * ROW_H
        crop = im.crop((CAPTION_X[0], y, CAPTION_X[1], y + ROW_H))
        if ImageOps.grayscale(crop).getextrema()[1] < 90:
            continue
        caption = text(crop)
        if caption:
            rows.append(caption)
    return {"title": title, "rows": rows}


if __name__ == "__main__":
    out = {}
    for path in sorted(Path(sys.argv[1]).glob("f*.png")):
        out[path.stem] = read(path)
    Path(sys.argv[2]).write_text(json.dumps(out, indent=0))
    print(len(out))
