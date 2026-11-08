"""Local recognition of the hand-filled drink list.

Pipeline per scanned page:
1. Render the page and split pixels into blue pen ink and printed (black/grey) content by colour.
2. Detect the printed underlines; every underline is one row of the sheet.
3. Read the printed "@Surname Name@" label of every row with Tesseract (ink is erased first)
   and match it against the expected list of names.
4. Cut the ink of every row into separate glyphs and classify each glyph with a small CNN
   that only has to choose between the drink characters.

Everything that is not certain (low classifier confidence, crossed-out glyph, row without a
recognised name) is reported, never silently dropped.
"""
import difflib
import os
from dataclasses import dataclass, field

import cv2
import fitz  # PyMuPDF
import numpy as np
import pytesseract
from unidecode import unidecode

RENDER_ZOOM = 3.0  # 216 DPI, enough for pen strokes and fast enough for Tesseract

# Ink is "blue enough" in Lab (b channel < 128 means blue) and clearly darker than the paper
INK_MAX_LAB_B = 119
INK_MIN_DARKNESS = 20
PRINT_MIN_DARKNESS = 35

DEFAULT_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "letter_cnn.pt")
EMNIST_CLASSES = [chr(ord("A") + i) for i in range(26)]


@dataclass
class Glyph:
    bbox: tuple  # x0, y0, x1, y1 in rendered page pixels
    char: str = None  # accepted character or None when uncertain
    guess: str = None  # best guess among the allowed characters
    confidence: float = 0.0
    crossed_out: bool = False
    image: np.ndarray = None  # 28x28 classifier input, for debugging


@dataclass
class Row:
    page: int
    line: tuple  # slope, intercept, x0, x1 of the underline
    label_text: str = ""
    name: str = None
    name_score: float = 0.0
    name_inferred: bool = False
    label_end: float = 0.0  # x where the printed label ends; handwriting is right of it
    glyphs: list = field(default_factory=list)


@dataclass
class PageResult:
    index: int
    image: np.ndarray
    rows: list
    stray_glyphs: list  # ink that does not belong to any row


# ---------------------------------------------------------------------------
# Rendering and colour separation
# ---------------------------------------------------------------------------

def render_pages(pdf_path, zoom=RENDER_ZOOM):
    doc = fitz.open(pdf_path)
    try:
        for page in doc:
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            img = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3]
            yield cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    finally:
        doc.close()


def split_ink_and_print(img_bgr):
    """Return (ink_mask, print_mask) as uint8 0/255 images."""
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    lightness = lab[:, :, 0].astype(np.int16)
    blue = lab[:, :, 2].astype(np.int16)
    # Local paper brightness compensates for shadows and uneven lighting of phone photos
    paper = cv2.medianBlur(lab[:, :, 0], 51).astype(np.int16)
    darkness = paper - lightness

    ink = ((darkness > INK_MIN_DARKNESS) & (blue < INK_MAX_LAB_B)).astype(np.uint8) * 255
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    printed = ((darkness > PRINT_MIN_DARKNESS) & (blue >= INK_MAX_LAB_B)).astype(np.uint8) * 255
    # The dark core of a pen stroke is not very blue; never count pixels next to ink as print
    printed[cv2.dilate(ink, np.ones((9, 9), np.uint8)) > 0] = 0
    return ink, printed


# ---------------------------------------------------------------------------
# Row geometry
# ---------------------------------------------------------------------------

def detect_underlines(print_mask):
    """Find long, nearly horizontal printed lines. Returns sorted list of (slope, intercept, x0, x1)."""
    h, w = print_mask.shape
    # Drop thick dark areas (table, shadows, background around the paper): underlines are thin
    thick = cv2.morphologyEx(print_mask, cv2.MORPH_OPEN, np.ones((11, 11), np.uint8))
    thick = cv2.dilate(thick, np.ones((7, 7), np.uint8))
    thin = cv2.bitwise_and(print_mask, cv2.bitwise_not(thick))
    # Short kernel tolerates a few degrees of tilt; long lines are assembled from the pieces below
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, w // 40), 1))
    horizontal = cv2.morphologyEx(thin, cv2.MORPH_OPEN, kernel)
    horizontal = cv2.dilate(horizontal, cv2.getStructuringElement(cv2.MORPH_RECT, (w // 60, 3)))

    count, labels, stats, _ = cv2.connectedComponentsWithStats(horizontal)
    segments = []
    for i in range(1, count):
        x, y, cw, ch, _ = stats[i]
        if cw < w * 0.25 or ch > cw * 0.15:
            continue
        ys, xs = np.nonzero(labels[y:y + ch, x:x + cw] == i)
        slope, intercept = np.polyfit(xs + x, ys + y, 1)
        if abs(slope) > 0.15:
            continue
        segments.append((slope, intercept, x, x + cw))

    segments.sort(key=lambda s: s[0] * w / 2 + s[1])
    return merge_close_lines(segments, w)


def merge_close_lines(segments, width):
    """Merge pieces of one underline that were detected as separate segments."""
    if len(segments) < 2:
        return segments
    ys = [line_y(s, width / 2) for s in segments]
    pitch = float(np.median(np.diff(ys)))
    merged = [segments[0]]
    for seg in segments[1:]:
        prev = merged[-1]
        x_mid = (max(prev[2], seg[2]) + min(prev[3], seg[3])) / 2
        if abs(line_y(prev, x_mid) - line_y(seg, x_mid)) < pitch * 0.4:
            # Keep the slope of the longer piece and extend the x range
            longer = prev if prev[3] - prev[2] >= seg[3] - seg[2] else seg
            merged[-1] = (longer[0], longer[1], min(prev[2], seg[2]), max(prev[3], seg[3]))
        else:
            merged.append(seg)
    return merged


def line_y(line, x):
    slope, intercept, _, _ = line
    return slope * x + intercept


def row_pitch(lines, width):
    if len(lines) < 2:
        return 40 * RENDER_ZOOM * 0.75
    ys = [line_y(l, width / 2) for l in lines]
    return float(np.median(np.diff(ys)))


# ---------------------------------------------------------------------------
# Labels (printed names)
# ---------------------------------------------------------------------------

def normalize_label(text):
    return "".join(c for c in unidecode(text).lower() if c.isalpha())


def label_from_ocr(text):
    """Keep only the part between the first and the last '@' (the printed label)."""
    if text.count("@") >= 2:
        return text[text.index("@") + 1:text.rindex("@")]
    return text


def read_label(img_bgr, ink_mask, line, pitch):
    """OCR the printed label that sits on the left part of the underline."""
    _, _, x0, x1 = line
    left = max(0, int(x0 - pitch * 0.3))
    right = int(min(img_bgr.shape[1], x0 + (x1 - x0) * 0.75))
    y_mid = line_y(line, (left + right) / 2)
    top = max(0, int(y_mid - pitch * 0.85))
    bottom = min(img_bgr.shape[0], int(y_mid + pitch * 0.1))

    crop = cv2.cvtColor(img_bgr[top:bottom, left:right], cv2.COLOR_BGR2GRAY)
    # Erase handwriting so it cannot merge with the printed label
    ink = cv2.dilate(ink_mask[top:bottom, left:right], np.ones((5, 5), np.uint8))
    crop = crop.copy()
    crop[ink > 0] = 255
    text = pytesseract.image_to_string(crop, config="--psm 7")
    return text.strip()


def match_label(text, expected_names):
    """Return (name, score) of the best matching expected name."""
    norm = normalize_label(label_from_ocr(text))
    if not norm:
        return None, 0.0
    best, best_score = None, 0.0
    for name in expected_names:
        candidate = normalize_label(name)
        # The label may be followed by noise, so compare the same-length prefix as well
        score = max(difflib.SequenceMatcher(None, norm, candidate).ratio(),
                    difflib.SequenceMatcher(None, norm[:len(candidate)], candidate).ratio())
        if score > best_score:
            best, best_score = name, score
    return best, best_score


def row_ordinals(rows, pitch, width):
    """Row number of every underline counted from the first one (tolerates a missing line)."""
    ordinals = [0]
    for prev, row in zip(rows, rows[1:]):
        gap = line_y(row.line, width / 2) - line_y(prev.line, width / 2)
        ordinals.append(ordinals[-1] + max(1, int(round(gap / pitch))))
    return ordinals


def assign_names(rows, sheet_pages, pitch, width, min_score=0.75):
    """Name every row from the printed labels and the known layout of the generated sheet.

    sheet_pages: list of pages, each a list of labels in printed order. Labels Tesseract reads
    confidently vote for the position of the page within the sheet; rows it could not read get
    their name from that position. Rows outside the sheet page (paper edge, other paper) are dropped.
    """
    sheet = [name for page in sheet_pages for name in page]
    chunk_of = [i for i, page in enumerate(sheet_pages) for _ in page]
    ordinals = row_ordinals(rows, pitch, width)

    votes = {}
    for row, ordinal in zip(rows, ordinals):
        name, score = match_label(row.label_text, sheet)
        if score < min_score:
            continue
        row.name_score = score
        for idx, candidate in enumerate(sheet):
            if candidate == name:
                base = idx - ordinal
                votes[base] = votes.get(base, 0) + score
    if not votes:
        return []

    base = max(votes, key=votes.get)
    chunks = [chunk_of[base + o] for o in ordinals if 0 <= base + o < len(sheet)]
    chunk = max(set(chunks), key=chunks.count)

    named = []
    for row, ordinal in zip(rows, ordinals):
        idx = base + ordinal
        if not (0 <= idx < len(sheet)) or chunk_of[idx] != chunk:
            continue
        row.name = sheet[idx]
        ocr_name, score = match_label(row.label_text, sheet)
        row.name_inferred = not (score >= min_score and ocr_name == row.name)
        named.append(row)
    return named


def label_right_edge(print_mask, line, pitch):
    """x where the printed label ends: the first wide gap in printed content above the underline."""
    _, _, x0, x1 = line
    x0, x1 = int(x0), int(x1)
    columns = []
    for x in range(x0, x1):
        y = line_y(line, x)
        top, bottom = int(y - pitch * 0.7), int(y - pitch * 0.12)
        columns.append(print_mask[max(0, top):max(0, bottom), x].any())
    gap_limit = int(pitch * 0.45)
    last_printed, gap = x0, 0
    for i, printed in enumerate(columns):
        if printed:
            last_printed, gap = x0 + i, 0
        else:
            gap += 1
            if gap > gap_limit and last_printed > x0:
                break
    return last_printed


# ---------------------------------------------------------------------------
# Glyph segmentation
# ---------------------------------------------------------------------------

def segment_glyphs(ink_mask, pitch):
    """Split the ink into glyph bounding boxes (one handwritten character each)."""
    min_size = max(4, int(pitch * 0.06))
    count, _, stats, _ = cv2.connectedComponentsWithStats(ink_mask)
    boxes = []
    for i in range(1, count):
        x, y, w, h, area = stats[i]
        if max(w, h) < min_size or area < min_size * 2:
            continue
        boxes.append([x, y, x + w, y + h])

    # Merge strokes of one character: they overlap horizontally and are vertically close
    merged = True
    while merged:
        merged = False
        boxes.sort()
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                overlap = min(a[2], b[2]) - max(a[0], b[0])
                narrower = min(a[2] - a[0], b[2] - b[0])
                gap_x = max(a[0], b[0]) - min(a[2], b[2])
                gap_y = max(a[1], b[1]) - min(a[3], b[3])
                same_char = ((overlap > 0.4 * narrower) or gap_x < pitch * 0.04) and gap_y < pitch * 0.25
                if same_char:
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    # Characters are at least a quarter of a row tall; flat or tiny boxes are dots, commas or line remnants
    boxes = [b for b in boxes if (b[3] - b[1]) >= pitch * 0.25 and (b[2] - b[0]) <= (b[3] - b[1]) * 4]
    result = []
    for b in boxes:
        result.extend(split_wide_box(ink_mask, b))
    return result


def split_wide_box(ink_mask, box, max_ratio=1.3, part_ratio=0.75):
    """Split touching letters (e.g. "TC" joined by a crossbar) at the thinnest columns."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= h * max_ratio:
        return [tuple(box)]
    parts = max(2, int(round(w / (h * part_ratio))))
    profile = (ink_mask[y0:y1, x0:x1] > 0).sum(axis=0)
    cuts = []
    for k in range(1, parts):
        ideal = int(w * k / parts)
        window = max(2, int(w / parts * 0.3))
        lo, hi = max(1, ideal - window), min(w - 1, ideal + window)
        cuts.append(lo + int(np.argmin(profile[lo:hi])))
    edges = [0] + cuts + [w]
    result = []
    for a, b in zip(edges, edges[1:]):
        rows = np.nonzero((ink_mask[y0:y1, x0 + a:x0 + b] > 0).any(axis=1))[0]
        if b - a > 2 and len(rows):
            result.append((x0 + a, y0 + int(rows[0]), x0 + b, y0 + int(rows[-1]) + 1))
    return result


def assign_glyphs_to_rows(boxes, rows, pitch):
    """A glyph belongs to the nearest underline below its centre (it may cross the line a bit)."""
    stray = []
    for box in boxes:
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        best, best_dist = None, None
        for row in rows:
            _, _, x0, x1 = row.line
            if cx < row.label_end - pitch * 0.1 or cx > x1 + pitch * 0.5:
                continue
            dist = line_y(row.line, cx) - cy
            if -pitch * 0.25 < dist < pitch * 1.0 and (best_dist is None or dist < best_dist):
                best, best_dist = row, dist
        if best is None:
            stray.append(Glyph(bbox=box))
        elif best_dist < pitch * 0.12 and (box[3] - box[1]) < pitch * 0.6:
            # Small mark sitting on or below the line: comma or dot separating characters
            continue
        else:
            best.glyphs.append(Glyph(bbox=box))
    for row in rows:
        row.glyphs.sort(key=lambda g: g.bbox[0])
    return stray


def glyph_to_emnist(ink_mask, bbox, pitch):
    """Convert a glyph to the 28x28 EMNIST format (white on black, centred, 2px border)."""
    x0, y0, x1, y1 = bbox
    crop = ink_mask[y0:y1, x0:x1].astype(np.float32) / 255.0
    # Normalise stroke width to roughly what EMNIST letters have
    stroke = max(1, int(round(pitch * 0.03)))
    crop = cv2.dilate(crop, np.ones((stroke, stroke), np.uint8))
    h, w = crop.shape
    side = max(h, w)
    square = np.zeros((side, side), np.float32)
    square[(side - h) // 2:(side - h) // 2 + h, (side - w) // 2:(side - w) // 2 + w] = crop
    square = cv2.GaussianBlur(square, (0, 0), sigmaX=side / 28.0)
    small = cv2.resize(square, (24, 24), interpolation=cv2.INTER_AREA)
    out = np.zeros((28, 28), np.float32)
    out[2:26, 2:26] = small
    if out.max() > 0:
        out /= out.max()
    return out


def fill_ratio(ink_mask, bbox):
    x0, y0, x1, y1 = bbox
    crop = ink_mask[y0:y1, x0:x1]
    return float((crop > 0).mean()) if crop.size else 0.0


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def build_model():
    import torch.nn as nn
    return nn.Sequential(
        nn.Conv2d(1, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(),
        nn.Conv2d(32, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(), nn.MaxPool2d(2),
        nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
        nn.Conv2d(64, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(), nn.MaxPool2d(2),
        nn.Flatten(), nn.Dropout(0.3),
        nn.Linear(64 * 7 * 7, 256), nn.ReLU(), nn.Dropout(0.3),
        nn.Linear(256, len(EMNIST_CLASSES)),
    )


class LetterClassifier:
    def __init__(self, model_path=DEFAULT_MODEL_PATH):
        import torch
        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"Letter model not found at '{model_path}'. Train it first: python train_letter_model.py")
        self.torch = torch
        self.model = build_model()
        self.model.load_state_dict(torch.load(model_path, map_location="cpu"))
        self.model.eval()

    def predict(self, images, allowed_chars):
        """Return list of {letter: probability} restricted to allowed_chars (renormalised)."""
        if not images:
            return []
        torch = self.torch
        batch = torch.from_numpy(np.stack(images)[:, None, :, :].astype(np.float32))
        with torch.no_grad():
            probs = torch.softmax(self.model(batch), dim=1).numpy()
        allowed_idx = [EMNIST_CLASSES.index(c) for c in allowed_chars]
        results = []
        for p in probs:
            sub = p[allowed_idx]
            total = sub.sum()
            sub = sub / total if total > 0 else np.full(len(sub), 1.0 / len(sub))
            results.append({c: float(v) for c, v in zip(allowed_chars, sub)})
        return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_scan(pdf_path, sheet_pages, allowed_chars, classifier=None, min_confidence=0.85,
                 crossed_fill_ratio=0.45, narrow_ratio=0.3):
    """Recognise all pages of a scanned drink list.

    sheet_pages: labels exactly as printed on the generated sheet, one list per sheet page.
    allowed_chars: characters used for drinks (case-insensitive).
    """
    allowed = sorted({c.upper() for c in allowed_chars if c.strip() and c.upper() in EMNIST_CLASSES})
    classifier = classifier or LetterClassifier()
    pages = []

    for page_index, img in enumerate(render_pages(pdf_path)):
        ink, printed = split_ink_and_print(img)
        lines = detect_underlines(printed)
        pitch = row_pitch(lines, img.shape[1])
        rows = [Row(page=page_index, line=line) for line in lines]
        for row in rows:
            row.label_text = read_label(img, ink, row.line, pitch)
            row.label_end = label_right_edge(printed, row.line, pitch)
        # Rows outside the sheet page (paper edge, another paper) are dropped here
        rows = assign_names(rows, sheet_pages, pitch, img.shape[1])

        boxes = segment_glyphs(ink, pitch)
        stray = assign_glyphs_to_rows(boxes, rows, pitch)

        glyphs = [g for r in rows for g in r.glyphs]
        to_classify = []
        for g in glyphs:
            if fill_ratio(ink, g.bbox) > crossed_fill_ratio:
                g.crossed_out = True
            else:
                g.image = glyph_to_emnist(ink, g.bbox, pitch)
                to_classify.append(g)
        for g, probs in zip(to_classify, classifier.predict([g.image for g in to_classify], allowed)):
            g.guess = max(probs, key=probs.get)
            g.confidence = probs[g.guess]
            x0, y0, x1, y1 = g.bbox
            # A bare vertical stroke is ambiguous (T with a lost crossbar, L, I): always ask
            too_narrow = (x1 - x0) < (y1 - y0) * narrow_ratio
            if g.confidence >= min_confidence and not too_narrow:
                g.char = g.guess

        pages.append(PageResult(index=page_index, image=img, rows=rows, stray_glyphs=stray))
    return pages


def annotate_pdf(pages, output_pdf_path):
    """Write a PDF with the recognised rows and glyphs drawn over each page."""
    doc = fitz.open()
    for page in pages:
        img = page.image.copy()
        for row in page.rows:
            slope, intercept, x0, x1 = row.line
            cv2.line(img, (int(x0), int(slope * x0 + intercept)), (int(x1), int(slope * x1 + intercept)),
                     (200, 120, 0), 2)
            label = f"{row.name}{' (?)' if row.name_inferred else ''}"
            cv2.putText(img, label, (int(x0), int(slope * x0 + intercept) + 22), cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (200, 120, 0), 2)
            for g in row.glyphs:
                gx0, gy0, gx1, gy1 = g.bbox
                if g.crossed_out:
                    color, text = (128, 128, 128), "X"
                elif g.char:
                    color, text = (0, 170, 0), f"{g.char} {g.confidence:.0%}"
                else:
                    color, text = (0, 0, 230), f"{g.guess}? {g.confidence:.0%}"
                cv2.rectangle(img, (gx0, gy0), (gx1, gy1), color, 2)
                cv2.putText(img, text, (gx0, gy0 - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        for g in page.stray_glyphs:
            gx0, gy0, gx1, gy1 = g.bbox
            cv2.rectangle(img, (gx0, gy0), (gx1, gy1), (0, 140, 255), 2)
            cv2.putText(img, "mimo radek", (gx0, gy0 - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 140, 255), 2)

        ok, png = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        pdf_page = doc.new_page(width=img.shape[1], height=img.shape[0])
        pdf_page.insert_image(pdf_page.rect, stream=png.tobytes())
    doc.save(output_pdf_path)
    doc.close()
    return output_pdf_path
