import customtkinter as ctk
from customtkinter import filedialog
import csv
from reportlab.pdfgen import canvas
from unidecode import unidecode
import os
import subprocess
import sys
from azure.cognitiveservices.vision.computervision import ComputerVisionClient
from msrest.authentication import CognitiveServicesCredentials
import time
import re
import json
from urllib.parse import urlencode
from dotenv import load_dotenv
import pprint
from PyPDF2 import PdfReader, PdfWriter
from io import BytesIO
from json import dump
import base64
from pdf2image import convert_from_path
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import letter
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import fitz  # PyMuPDF
from handwriting_ocr import (LetterClassifier, analyze_scan, annotate_pdf, encode_jpeg_within, final_pdf,
                             person_row_images)

load_dotenv()

PREFERENCES_FILE = os.getenv("PREFERENCES_FILE", "preferences.json")
DRINKS_FILE = os.getenv("DRINKS_FILE", "drinks.json")
INSTRUCTIONS_FILE = os.getenv("INSTRUCTIONS_FILE", "instructions.json")
DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
# "local" = handwriting_ocr (Tesseract + own letter model), "azure" = Azure Read OCR
OCR_ENGINE = os.getenv("OCR_ENGINE", "local").lower()
# Teams messages sent by the Power Automate Flow bot are size limited: in a test a 19.3 kB message
# with an inline image went through, a 28.5 kB one did not. The row image is shrunk to stay below this.
TEAMS_MESSAGE_MAX_BYTES = int(os.getenv("TEAMS_MESSAGE_MAX_BYTES", 19 * 1024))

class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def load_drinks():
    if os.path.exists(DRINKS_FILE):
        with open(DRINKS_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    return []

def save_drinks(drinks):
    with open(DRINKS_FILE, "w", encoding="utf-8") as file:
        json.dump(drinks, file, ensure_ascii=False, indent="\t")

DEFAULTS = {
    "bank_account": " ",
    "lines_per_person": 2,
    "max_persons_per_page": 18,
    "copies": 1,
    "program_files_folder": "program_files",
    "qr_code_server_url": "https://api.paylibo.com/paylibo",
    "event_name": "",
    "organizer_name": "",
    "message_template": "Ahoj, {event_name} je u konce a to znamená jediné. Přišel čas zúčtování.<br><br>Níže posílám tvůj přehled:<br><ul>{drinks_summary}</ul><b>Celkem: {total_price} Kč</b><br><br>Platbu můžeš poslat na účet <b>{bank_account}</b> nebo naskenovat QR kód:<br>{qr_code_url}<br><br>A pokud chceš ocenit moji snahu, dýšku se bránit nebudu. 😄<br><br>Díky a na další akci!<br><br><small><i>Analýza nápojů a rozesílání zpráv proběhly automaticky. V případě dotazů nebo reklamací kontaktuj {organizer_name}.</i></small>"
}

# Texts of the instruction page. Placeholders: {lines_per_person}, {lines_word}, {event_name}, {organizer_name}, {price}
DEFAULT_INSTRUCTIONS = {
    "title": "Pití na teambuildingu",
    "subtitle": "{event_name}",
    "rules": [
        "Pití najdeš na třech místech: v lednici v suterénu hlavní budovy, v jídelně a ve společenské místnosti.",
        "Vezmi si, na co máš chuť, a do svého řádku připiš znak z tabulky dole. Co kus, to jeden znak – třeba dvě piva a kofola = PPK.",
        "U pití jsou volně k dispozici půllitrové kelímky – ber si, kolik potřebuješ.",
        "Piš VELKÝMI TISKACÍMI PÍSMENY. Papíry po akci čte počítač (OCR) a na krasopis moc není.",
        "Každý má {lines_per_person} {lines_word}. Když ti dojde místo nebo na seznamu chybíš, připiš se dolů jako @Prijmeni Jmeno@ – bez háčků a čárek a i se zavináči.",
        "Když se spleteš, špatný znak celý začerni, ať z něj zbyde jen černý čtvereček – jako u přijímaček. Jen přeškrtnutý by ho počítač pořád přečetl.",
        "Po akci ti přijde přehled útraty a QR kód na zaplacení."
    ],
    "lines_word_forms": [
        "řádek",
        "řádky",
        "řádků"
    ],
    "menu_title": "Co si můžeš dát",
    "column_name": "Pití",
    "column_price": "Cena",
    "column_characters": "Znak",
    "price_format": "{price} Kč",
    "footer": "O pití se stará {organizer_name}"
}

def load_instructions():
    """Load instruction texts; create the file with defaults when it does not exist yet."""
    instructions = dict(DEFAULT_INSTRUCTIONS)
    if os.path.exists(INSTRUCTIONS_FILE):
        with open(INSTRUCTIONS_FILE, "r", encoding="utf-8") as file:
            instructions.update(json.load(file))
    else:
        with open(INSTRUCTIONS_FILE, "w", encoding="utf-8") as file:
            json.dump(DEFAULT_INSTRUCTIONS, file, ensure_ascii=False, indent="\t")
    return instructions

class _KeepMissing(dict):
    """format_map helper that leaves unknown {placeholders} untouched instead of failing."""
    def __missing__(self, key):
        return "{" + key + "}"

def fill_placeholders(template, **values):
    return template.format_map(_KeepMissing(values))

def load_preferences():
    prefs = dict(DEFAULTS)
    if os.path.exists(PREFERENCES_FILE):
        with open(PREFERENCES_FILE, "r", encoding="utf-8") as file:
            prefs.update(json.load(file))
    return prefs

def save_preferences(preferences):
    with open(PREFERENCES_FILE, "w", encoding="utf-8") as file:
        json.dump(preferences, file)


def is_decorative_line(text):
    """
    Check if a line of text is decorative (like underscores) rather than actual content.
    
    Args:
        text: The text to check
    
    Returns:
        bool: True if the text is decorative, False otherwise
    """
    # Check if the text consists only of underscores, dashes, or similar characters
    if not text.strip():
        return True  # Empty line
    
    decorative_chars = "_-="
    return all(c in decorative_chars for c in text.strip())

# Czech alphabet order; č, ř, š, ž and "ch" are separate letters
CZECH_ALPHABET = ["a", "b", "c", "č", "d", "e", "f", "g", "h", "ch", "i", "j", "k", "l", "m",
                  "n", "o", "p", "q", "r", "ř", "s", "š", "t", "u", "v", "w", "x", "y", "z", "ž"]
CZECH_RANK = {letter: rank for rank, letter in enumerate(CZECH_ALPHABET)}
# Letters whose accent only matters as a tie-breaker (sorted like their base letter)
CZECH_SECONDARY = {"á": "a", "ď": "d", "é": "e", "ě": "e", "í": "i", "ň": "n",
                   "ó": "o", "ť": "t", "ú": "u", "ů": "u", "ý": "y"}

def czech_sort_key(text):
    """Return a sort key following Czech alphabetical order (z < ž, h < ch < i)."""
    text = text.casefold()
    primary = []
    secondary = []
    i = 0
    while i < len(text):
        if text[i:i + 2] == "ch":
            primary.append(CZECH_RANK["ch"])
            secondary.append(0)
            i += 2
            continue
        char = text[i]
        if char.isspace():
            # Word boundary sorts before any letter ("Novák Jan" < "Nováková Eva")
            primary.append(-1)
            secondary.append(0)
        elif char in CZECH_RANK:
            primary.append(CZECH_RANK[char])
            secondary.append(0)
        elif char in CZECH_SECONDARY:
            primary.append(CZECH_RANK[CZECH_SECONDARY[char]])
            secondary.append(1)
        else:
            # Foreign accented letters fall back to their ASCII base, other chars go last
            base = unidecode(char).casefold()[:1]
            primary.append(CZECH_RANK.get(base, len(CZECH_ALPHABET) + ord(char)))
            secondary.append(0 if base == char else 1)
        i += 1
    return (primary, secondary, text)

# TTF fonts with Czech glyphs (built-in Helvetica cannot render č, ř, ž, ...)
FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
]

def register_unicode_fonts():
    """Register a regular/bold TTF pair with Czech glyphs. Returns (regular, bold, supports_unicode)."""
    for regular_path, bold_path in FONT_CANDIDATES:
        if os.path.exists(regular_path) and os.path.exists(bold_path):
            if "Unicode" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("Unicode", regular_path))
                pdfmetrics.registerFont(TTFont("Unicode-Bold", bold_path))
            return "Unicode", "Unicode-Bold", True
    print(f"{bcolors.WARNING}No TTF font with Czech glyphs found, instructions will be without diacritics.{bcolors.ENDC}")
    return "Helvetica", "Helvetica-Bold", False

def czech_plural(count, one, few, many):
    """Pick the Czech plural form: 1 řádek, 2-4 řádky, 5+ řádků."""
    if count == 1:
        return one
    if 2 <= count <= 4:
        return few
    return many

def wrap_czech_text(text, font_name, font_size, max_width):
    """Wrap text to max_width; single-letter words (v, k, s, z, a, ...) never end a line."""
    # Glue each single-letter word to the word that follows it
    groups = []
    pending = ""
    for word in text.split():
        if len(word) == 1 and word.isalpha():
            pending += word + " "
        else:
            groups.append(pending + word)
            pending = ""
    if pending:
        groups.append(pending.strip())

    lines = []
    current = ""
    for group in groups:
        candidate = f"{current} {group}" if current else group
        if current and pdfmetrics.stringWidth(candidate, font_name, font_size) > max_width:
            lines.append(current)
            current = group
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines

def draw_instruction_page(c, drinks, instructions, lines_per_person, event_name="", organizer_name=""):
    """Draw one A4 page with instructions for participants and the drink menu."""
    regular, bold, unicode_ok = register_unicode_fonts()
    text = (lambda t: t) if unicode_ok else unidecode
    page_width, page_height = A4
    margin = 50
    content_width = page_width - 2 * margin
    c.setFillColorRGB(0, 0, 0)

    # Title
    y = page_height - 80
    c.setFont(bold, 30)
    lines_word = czech_plural(lines_per_person, *instructions["lines_word_forms"])
    fill = lambda template: text(fill_placeholders(template, lines_per_person=lines_per_person,
                                                   lines_word=lines_word, event_name=event_name,
                                                   organizer_name=organizer_name))
    c.drawCentredString(page_width / 2, y, fill(instructions["title"]))
    subtitle = fill(instructions["subtitle"]).strip()
    if subtitle:
        y -= 26
        c.setFont(regular, 15)
        c.drawCentredString(page_width / 2, y, subtitle)

    # Rules
    rules = [fill(rule) for rule in instructions["rules"]]
    font_size = 14
    line_height = 20
    bullet_indent = 16
    y -= 45
    c.setFont(regular, font_size)
    for rule in rules:
        wrapped = wrap_czech_text(rule, regular, font_size, content_width - bullet_indent)
        c.drawString(margin, y, "•")
        for line in wrapped:
            c.drawString(margin + bullet_indent, y, line)
            y -= line_height
        y -= 6

    # Drink menu
    y -= 25
    c.setFont(bold, 24)
    c.drawString(margin, y, fill(instructions["menu_title"]))
    y -= 35

    col_name = margin
    col_price = margin + content_width * 0.62
    col_char = margin + content_width * 0.80
    row_height = 26

    c.setFont(bold, font_size)
    c.drawString(col_name, y, fill(instructions["column_name"]))
    c.drawString(col_price, y, fill(instructions["column_price"]))
    c.drawString(col_char, y, fill(instructions["column_characters"]))
    y -= 8
    c.setLineWidth(1)
    c.line(margin, y, page_width - margin, y)

    for drink in drinks:
        y -= row_height - 8
        c.setFont(regular, font_size)
        c.drawString(col_name, y, text(drink["name"]))
        c.drawString(col_price, y, text(fill_placeholders(instructions["price_format"], price=drink["price"])))
        c.setFont(bold, font_size + 4)
        c.drawString(col_char, y, ", ".join(drink.get("characters", [])))
        y -= 8
        c.setLineWidth(0.3)
        c.line(margin, y, page_width - margin, y)

    # Small footer in the bottom right corner (e.g. who takes care of the drinks)
    footer = fill(instructions.get("footer", "")).strip()
    if footer:
        c.setFont(regular, 10)
        c.setFillColorRGB(0.35, 0.35, 0.35)
        c.drawRightString(page_width - margin, 40, footer)
        c.setFillColorRGB(0, 0, 0)


def get_all_drink_characters(drinks):
    chars = set(" ")
    for drink in drinks:
        for c in drink.get("characters", []):
            chars.add(c)
    return chars

def contains_only_drink_characters(text, drinks=None):
    clean_text = text.strip()
    if not clean_text:
        return False
    if drinks is None:
        drinks = load_drinks()
    drink_chars = get_all_drink_characters(drinks)
    return all(c in drink_chars for c in clean_text)

def draw_bounding_boxes_on_pdf(input_pdf_path, output_pdf_path, bounding_boxes, persons_list=None):
    """
    Draw bounding boxes on the PDF based on OCR results.
    
    Args:
        input_pdf_path: Path to the original PDF
        output_pdf_path: Path to save the annotated PDF
        bounding_boxes: Dictionary with page number as key and list of (text, box_coords, width, height) tuples as value
        persons_list: Optional list of person dictionaries to check for name matches
    """
    # Open the PDF with PyMuPDF (fitz)
    doc = fitz.open(input_pdf_path)
    
    # Process each page
    for page_num, boxes_data in bounding_boxes.items():
        if page_num >= len(doc):
            continue
            
        page = doc[page_num]
        pdf_width = page.rect.width
        pdf_height = page.rect.height
        
        # Draw each bounding box
        for text, box, ocr_width, ocr_height in boxes_data:
            # Skip drawing bounding boxes for decorative lines
            if is_decorative_line(text):
                continue
                
            # Scale coordinates from OCR coordinate system to PDF coordinate system
            scaled_box = []
            for i in range(0, len(box), 2):
                # x coordinates are at even indices
                scaled_box.append(box[i] / ocr_width * pdf_width)
                # y coordinates are at odd indices
                scaled_box.append(box[i+1] / ocr_height * pdf_height)
            
            # Extract scaled coordinates (x1, y1, x2, y2, x3, y3, x4, y4)
            # The points should be: top-left, top-right, bottom-right, bottom-left
            x1, y1 = scaled_box[0], scaled_box[1]  # Top-left
            x2, y2 = scaled_box[2], scaled_box[3]  # Top-right
            x3, y3 = scaled_box[4], scaled_box[5]  # Bottom-right
            x4, y4 = scaled_box[6], scaled_box[7]  # Bottom-left
            
            # For a perfect rectangle with vertical sides, use:
            # Find the min/max x and y values
            min_x = min(x1, x4)
            max_x = max(x2, x3)
            min_y = min(y1, y2)
            max_y = max(y3, y4)
            
            # Create rectangle with exact vertical and horizontal lines
            rect = fitz.Rect(min_x, min_y, max_x, max_y)
            
            # Check if text is a match for a person name
            is_name_match = False
            if persons_list:
                # Clean the text by removing @ symbols which are used in the PDF template
                clean_text = text.strip().replace('@', '')
                
                # Check if this text matches any name in the persons list
                for person in persons_list:
                    if unidecode(person['name']).lower() == unidecode(clean_text).lower():
                        is_name_match = True
                        break
            
            # Choose color based on text content
            if is_name_match:
                # Use blue color for name matches (RGB: 0,0,1)
                page.draw_rect(rect, color=(0, 0, 1), width=1.5)
            elif "Strana" in text:
                # Use grey color for page numbers (RGB: 0.5,0.5,0.5)
                page.draw_rect(rect, color=(0.5, 0.5, 0.5), width=1)
            
            elif contains_only_drink_characters(text):
                # Use green color for drink-tracking characters (RGB: 0,0.8,0)
                page.draw_rect(rect, color=(0, 0.8, 0), width=1.5)
            else:
                # Use red color for other text (RGB: 1,0,0)
                page.draw_rect(rect, color=(1, 0, 0), width=1)
            
            # Add text annotation above the box
            page.insert_text(fitz.Point(min_x, min_y - 5), text, color=(0, 0, 1), fontsize=8)
    
    # Save the annotated PDF
    doc.save(output_pdf_path)
    doc.close()
    
    print(f"PDF with bounding boxes saved to {output_pdf_path}")
    return output_pdf_path


AZURE_ENDPOINT = os.getenv('AZURE_ENDPOINT')
AZURE_API_KEY = os.getenv('AZURE_API_KEY')

def generate_czech_qr_code(server_url, account_prefix=None, account_number=None, bank_code=None,
                            amount=None, currency=None, vs=None, ks=None, ss=None, identifier=None, 
                            date=None, message=None, compress=True, branding=True, size=None):
    """
    Call the API to generate a Czech QR code for payment.

    :param server_url: Base URL of the API server.
    :param account_prefix: Account prefix (string).
    :param account_number: Account number (string).
    :param bank_code: Bank code (string).
    :param amount: Payment amount (float).
    :param currency: Payment currency (string).
    :param vs: Variable symbol (string).
    :param ks: Constant symbol (string).
    :param ss: Specific symbol (string).
    :param identifier: Internal payment ID (string).
    :param date: Due date in ISO 8601 format (YYYY-mm-dd).
    :param message: Message for the recipient (string).
    :param compress: Use compact format (boolean, default: True).
    :param branding: Use QR code branding (boolean, default: True).
    :param size: QR code size in pixels (integer).
    :return: Response object from the API.
    """
    url = f"{server_url}/generator/czech/image"

    # Prepare query parameters, omitting any that are None or blank
    params = {
        "accountPrefix": account_prefix,
        "accountNumber": account_number,
        "bankCode": bank_code,
        "amount": amount,
        "currency": currency,
        "vs": vs,
        "ks": ks,
        "ss": ss,
        "identifier": identifier,
        "date": date,
        "message": message,
        "compress": compress,
        "branding": branding,
        "size": size
    }

    # Remove keys with None or blank values
    params = {k: v for k, v in params.items() if v not in [None, ""]}

    
    query_string = urlencode(params)

    # Return the full URL
    return f"{url}?{query_string}"


def load_persons(file_path):
    """Load people from CSV rows "First [Middle] Surname;email".

    The sheet is printed and checked as "@Surname First [Middle]@", so the name is flipped into
    that form and used as the key ("Jan Novák" → "Novák Jan"). The original order is kept as
    full_name for messages. Persons are sorted by the key in Czech alphabetical order.
    """
    persons = []
    with open(file_path, newline='', encoding='utf-8') as csvfile:
        for row in csv.reader(csvfile, delimiter=';'):
            if len(row) >= 1 and row[0].strip():
                parts = row[0].split()
                persons.append({
                    'name': " ".join(parts[-1:] + parts[:-1]),
                    'full_name': " ".join(parts),
                    'email': row[1].strip() if len(row) >= 2 else "",
                })
    persons.sort(key=lambda p: czech_sort_key(p['name']))
    return persons


class PayMeADrink:
    def __init__(self):
        self.app = ctk.CTk()
        self.app.title("Generování pivního záznamu")
        

        self.top_frame = ctk.CTkFrame(self.app)
        self.top_frame.pack(padx=10, pady=10, fill="x")

        self.left_frame = ctk.CTkFrame(self.top_frame)
        self.left_frame.pack(side="left", padx=10, pady=10)

        self.right_frame = ctk.CTkFrame(self.top_frame)
        self.right_frame.pack(side="left", padx=10, pady=10)

        self.bottom_frame = ctk.CTkFrame(self.app)
        self.bottom_frame.pack(padx=10, pady=10, fill="x")

        self.settings_frame = ctk.CTkFrame(self.bottom_frame)
        self.settings_frame.pack(side="left", padx=10, pady=10, fill="x")
        
        self.earnings_frame = ctk.CTkFrame(self.bottom_frame)
        self.earnings_frame.pack(side="left", padx=120, pady=10, fill="x")    

        self.label = ctk.CTkLabel(self.left_frame, text="Generování seznamu", font=("Helvetica", 16, "bold"))
        self.label.pack(pady=10)

        self.file_button = ctk.CTkButton(self.left_frame, text="Prozkoumat", command=self.browse_file)
        self.file_button.pack(side="left", padx=10)

        self.generate_button = ctk.CTkButton(self.left_frame, text="Vygenerovat PDF", command=self.generate_pdf)
        self.generate_button.pack(side="left", padx=10)

        self.copies_label = ctk.CTkLabel(self.left_frame, text="Kopií:")
        self.copies_label.pack(side="left", padx=(10, 5))
        self.copies_entry = ctk.CTkEntry(self.left_frame, width=50)
        self.copies_entry.pack(side="left", padx=(0, 10))
        
        self.label_second = ctk.CTkLabel(self.right_frame, text="Poslání QR plateb.", font=("Helvetica", 16, "bold"))
        self.label_second.pack(pady=10)
        
        self.file_button_scan = ctk.CTkButton(self.right_frame, text="Vybrat sken papíru", command=self.browse_file_scan)
        self.file_button_scan.pack(side="left", padx=10)

        self.generate_json_with_payments = ctk.CTkButton(self.right_frame, text="Vygenerovat JSON s platbami", command=self.generate_json_with_payments)
        self.generate_json_with_payments.pack(side="left", padx=10)
        
        self.settings_label = ctk.CTkLabel(self.settings_frame, text="Nastavení cen a účtu", font=("Helvetica", 16, "bold"))
        self.settings_label.pack(pady=10)

        self.preferences = load_preferences()
        self.drinks = load_drinks()
        self.copies_entry.insert(0, str(self.preferences.get("copies", 1)))

        # Build a character-to-drink lookup for OCR matching
        self.char_to_drink = {}
        for drink in self.drinks:
            for c in drink.get("characters", []):
                self.char_to_drink[c] = drink["id"]

        # Dynamic drink price entries
        self.drink_price_entries = {}
        for drink in self.drinks:
            frame = ctk.CTkFrame(self.settings_frame)
            frame.pack(pady=5, fill="x")
            label = ctk.CTkLabel(frame, text=f"Cena za {drink['name']}:")
            label.pack(side="left", padx=5)
            entry = ctk.CTkEntry(frame)
            entry.insert(0, str(drink["price"]))
            entry.pack(side="left", padx=5)
            self.drink_price_entries[drink["id"]] = entry

        self.event_name_frame = ctk.CTkFrame(self.settings_frame)
        self.event_name_frame.pack(pady=5, fill="x")
        self.event_name_label = ctk.CTkLabel(self.event_name_frame, text="Název akce:")
        self.event_name_label.pack(side="left", padx=5)
        self.event_name_entry = ctk.CTkEntry(self.event_name_frame)
        self.event_name_entry.insert(0, self.preferences.get("event_name", ""))
        self.event_name_entry.pack(side="left", padx=5)

        self.organizer_name_frame = ctk.CTkFrame(self.settings_frame)
        self.organizer_name_frame.pack(pady=5, fill="x")
        self.organizer_name_label = ctk.CTkLabel(self.organizer_name_frame, text="Jméno organizátora:")
        self.organizer_name_label.pack(side="left", padx=5)
        self.organizer_name_entry = ctk.CTkEntry(self.organizer_name_frame)
        self.organizer_name_entry.insert(0, self.preferences.get("organizer_name", ""))
        self.organizer_name_entry.pack(side="left", padx=5)

        self.bank_account_frame = ctk.CTkFrame(self.settings_frame)
        self.bank_account_frame.pack(pady=5, fill="x")
        self.bank_account_label = ctk.CTkLabel(self.bank_account_frame, text="Číslo bankovního účtu:")
        self.bank_account_label.pack(side="left", padx=5)
        self.bank_account_entry = ctk.CTkEntry(self.bank_account_frame)
        self.bank_account_entry.insert(0, self.preferences.get("bank_account", " "))
        self.bank_account_entry.pack(side="left", padx=5)

        self.save_button = ctk.CTkButton(self.settings_frame, text="Uložit nastavení", command=self.save_preferences)
        self.save_button.pack(pady=10)

        self.earnings_label = ctk.CTkLabel(self.earnings_frame, text="Tržby", font=("Helvetica", 16, "bold"))
        self.earnings_label.pack(pady=10)

        # Dynamic drink sold labels
        self.drink_sold_values = {}
        for drink in self.drinks:
            frame = ctk.CTkFrame(self.earnings_frame)
            frame.pack(pady=5, fill="x")
            label = ctk.CTkLabel(frame, text=f"Celkem {drink['name']} prodáno:")
            label.pack(side="left", padx=5)
            value = ctk.CTkLabel(frame, text="0")
            value.pack(side="left", padx=5)
            self.drink_sold_values[drink["id"]] = value

        self.total_earnings_frame = ctk.CTkFrame(self.earnings_frame)
        self.total_earnings_frame.pack(pady=5, fill="x")
        self.total_earnings_label = ctk.CTkLabel(self.total_earnings_frame, text="Celkový výdělek:")
        self.total_earnings_label.pack(side="left", padx=5)
        self.total_earnings_value = ctk.CTkLabel(self.total_earnings_frame, text="0")
        self.total_earnings_value.pack(side="left", padx=5)

        self.total_unmatched_frame = ctk.CTkFrame(self.earnings_frame)
        self.total_unmatched_frame.pack(pady=5, fill="x")
        self.total_unmatched_label = ctk.CTkLabel(self.total_unmatched_frame, text="Neuznané znaky:")
        self.total_unmatched_label.pack(side="left", padx=5)
        self.total_unmatched_value = ctk.CTkLabel(self.total_unmatched_frame, text="0")
        self.total_unmatched_value.pack(side="left", padx=5)
        
        self.persons = []
        
        self.scanned_file_path = None
        self.scan_pages = None  # result of the local recognition, used for the final PDF
        
        self.app.mainloop()

    def browse_file(self):
        file_path = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv")])
        if file_path:
            filename = os.path.basename(file_path)
            self.file_button.configure(text=filename)

            self.persons = load_persons(file_path)
            print(bcolors.HEADER + bcolors.BOLD + "Loaded data:" + bcolors.ENDC)
            pprint.pprint(self.persons)
        else:
            self.file_button.configure(text="Prozkoumat")
            
    def browse_file_scan(self):
        file_path = filedialog.askopenfilename(filetypes=[
            ("All files", "*.*")  # Optionally allow any file type
        ])
        
        if file_path:
            filename = os.path.basename(file_path)
            self.file_button_scan.configure(text=filename)
            self.scanned_file_path = file_path
        else:
            self.file_button_scan.configure(text="Prozkoumat")

    def generate_pdf(self):
        try:
            copies = int(self.copies_entry.get().strip() or 1)
            if copies < 1:
                raise ValueError
        except ValueError:
            print(f"{bcolors.FAIL}Number of copies must be a whole number of at least 1.{bcolors.ENDC}")
            return
        self.preferences["copies"] = copies
        save_preferences(self.preferences)

        if not self.persons:
            print(f"{bcolors.WARNING}No persons loaded, generating instruction pages only.{bcolors.ENDC}")

        # Make sure the directory exists
        os.makedirs(self.preferences["program_files_folder"], exist_ok=True)

        file_name = os.path.join(self.preferences["program_files_folder"], "pivni_seznam.pdf")
        c = canvas.Canvas(file_name, pagesize=A4)

        # Reloaded on every generation, so edits to the JSON apply without restarting the app
        instructions = load_instructions()

        # Each copy = one instruction page followed by the whole list (page numbers restart per copy).
        # No copy label is printed on list pages: OCR reads the footer and its letters could match drinks.
        for _ in range(copies):
            draw_instruction_page(c, self.drinks, instructions, self.preferences["lines_per_person"],
                                  self.preferences.get("event_name", ""),
                                  self.preferences.get("organizer_name", ""))
            c.showPage()
            self.draw_person_list(c)

        c.save()
        print(f"PDF generated successfully as '{file_name}'.")
        
        # Open the generated PDF
        if sys.platform == "win32":
            os.startfile(file_name)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", file_name])
        else:
            subprocess.Popen(["xdg-open", file_name])
        
    def draw_person_list(self, c):
        """Draw the list of persons with lines for drink characters, one or more pages."""
        lines_per_person = self.preferences["lines_per_person"]
        duplicated_persons = []
        for person in self.persons:
            for i in range(lines_per_person):
                duplicated_persons.append(person)

        black_color = (0, 0, 0)
        grey_color = (0.4, 0.4, 0.4)
        page_number = 1
        y_position = 750  # Starting y position on the page

        for index, person in enumerate(duplicated_persons, start=1):
            c.setFont("Helvetica-Bold", 20)
            # Alternate color for each person (every lines_per_person rows)
            if (index - 1) // lines_per_person % 2 == 0:
                c.setFillColorRGB(*black_color)
            else:
                c.setFillColorRGB(*grey_color)

            max_number_of_underscores = 50
            c.drawString(20, y_position-3, "_" * max_number_of_underscores)
            c.drawString(20, y_position, f"@{unidecode(person['name'])}@")
            y_position -= 40  # Move down for the next entry

            if index % self.preferences["max_persons_per_page"] == 0 or index == len(duplicated_persons):
                # Add footer with page number
                c.setFillColorRGB(*black_color)
                c.drawString(265, 20, f"Strana {page_number}")
                page_number += 1
                c.showPage()  # Create a new page
                y_position = 750  # Reset y position for the new page

    def save_preferences(self):
        # Save drink prices to drinks.json
        for drink in self.drinks:
            entry = self.drink_price_entries.get(drink["id"])
            if entry:
                drink["price"] = int(entry.get())
        save_drinks(self.drinks)

        # Save preferences to preferences.json
        self.preferences["event_name"] = self.event_name_entry.get()
        self.preferences["organizer_name"] = self.organizer_name_entry.get()
        self.preferences["bank_account"] = self.bank_account_entry.get()
        save_preferences(self.preferences)
        print("Preferences saved. Drinks:", self.drinks, "Preferences:", self.preferences)
     
    def read_scan_azure(self):
        """Azure Read OCR (original engine). Returns [(name, [characters])] in sheet order."""
        api_key = AZURE_API_KEY
        endpoint = AZURE_ENDPOINT
        client = ComputerVisionClient(endpoint, CognitiveServicesCredentials(api_key))

        final_text = ""
        # Dictionary to store bounding boxes for each page
        all_bounding_boxes = {}

        # Read the original PDF
        original_pdf = PdfReader(open(self.scanned_file_path, "rb"))

        # Process each page individually
        for page_num in range(len(original_pdf.pages)):
            # Create a single-page PDF in memory
            writer = PdfWriter()
            writer.add_page(original_pdf.pages[page_num])

            byte_stream = BytesIO()
            writer.write(byte_stream)
            byte_stream.seek(0)  # Reset stream position to beginning

            # Send to Azure OCR
            try:
                response = client.read_in_stream(byte_stream, raw=True)
            except Exception as e:
                print(f"Error during OCR processing: {e}")

            # Get operation ID
            operation_location = response.headers["Operation-Location"]
            operation_id = operation_location.split("/")[-1]

            # Poll for results
            while True:
                result = client.get_read_result(operation_id)
                if result.status not in ["notStarted", "running"]:
                    break
                print(f"Waiting for page {page_num + 1} result...")
                time.sleep(1)

            # Extract text and bounding boxes
            page_boxes = []
            if result.status == "succeeded":
                for page in result.analyze_result.read_results:
                    ocr_width = page.width  # Get width from OCR result
                    ocr_height = page.height  # Get height from OCR result
                    
                    for line in page.lines:
                        final_text += line.text + "\n"
                        # Store bounding box information with page dimensions
                        bounding_box = line.bounding_box
                        page_boxes.append((line.text, bounding_box, ocr_width, ocr_height))
                        
                        # Print bounding box info for debugging
                        # print(f"OCR Text: {line.text}")
                        # print(f"Bounding Box: {bounding_box}")
                        # print(f"OCR Page Dimensions: {ocr_width} x {ocr_height}")
            else:
                print(f"Analysis failed for page {page_num + 1}")
            
            all_bounding_boxes[page_num] = page_boxes

        # Create annotated PDF with bounding boxes
        output_pdf_path = os.path.join(self.preferences["program_files_folder"], "annotated_scan.pdf")
        os.makedirs(self.preferences["program_files_folder"], exist_ok=True)
        annotated_pdf = draw_bounding_boxes_on_pdf(
            self.scanned_file_path,
            output_pdf_path,
            all_bounding_boxes,
            self.persons
        )

        if sys.platform == "win32":
            os.startfile(annotated_pdf)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", annotated_pdf])
        else:
            subprocess.Popen(["xdg-open", annotated_pdf])

        # Continue with existing processing
        if DEBUG:
            print(f"\n{bcolors.HEADER}=== RAW OCR TEXT ==={bcolors.ENDC}")
            print(repr(final_text[:500]))

        final_text = final_text.replace(" ", "").replace("-", "").replace("_", "").replace("Strana", "")
        final_text = ''.join([i for i in final_text if not i.isdigit()])
        final_text = final_text.split("@")[1:]

        if DEBUG:
            print(f"\n{bcolors.HEADER}=== PARSED SEGMENTS (split by @) ==={bcolors.ENDC}")
            for idx, segment in enumerate(final_text):
                label = "NAME" if idx % 2 == 0 else "DRINKS"
                print(f"  [{idx}] {label}: {repr(segment.strip())}")
            if self.persons:
                print(f"\n{bcolors.HEADER}=== LOADED PERSONS ({len(self.persons)}) ==={bcolors.ENDC}")
                for p in self.persons:
                    print(f"  - {p['name']} → unidecode: {unidecode(p['name'])}")

        entries = []
        for i in range(0, len(final_text) - 1, 2):
            name = " ".join(re.findall(r'[A-Z][a-z]*', final_text[i]))
            chars = list(final_text[i + 1])
            entries.append((name, chars, [None] * len(chars)))
        self.scan_pages = None
        return entries

    def sheet_pages(self):
        """Labels exactly as draw_person_list prints them, split into the pages of the sheet."""
        lines_per_person = self.preferences["lines_per_person"]
        labels = [unidecode(p['name']) for p in self.persons for _ in range(lines_per_person)]
        per_page = self.preferences["max_persons_per_page"]
        return [labels[i:i + per_page] for i in range(0, len(labels), per_page)]

    def open_file(self, path):
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def drink_char(self, drink_id):
        """Character shown for a drink in the final PDF (its first configured character)."""
        drink = next(d for d in self.drinks if d["id"] == drink_id)
        return drink["characters"][0].upper() if drink.get("characters") else drink_id

    def drink_for_char(self, char):
        """Drink id for a character; letters match regardless of case."""
        return (self.char_to_drink.get(char) or self.char_to_drink.get(char.upper())
                or self.char_to_drink.get(char.lower()))

    def read_scan_local(self):
        """Local recognition (handwriting_ocr). Returns [(name, [tokens])] in sheet order.

        A token is either a recognised drink character or a description of a glyph that has to be
        confirmed by hand (uncertain or crossed out); those end up as unmatched characters.
        """
        if not self.persons:
            print(f"{bcolors.FAIL}Load the CSV with people first - the names on the sheet are matched against it.{bcolors.ENDC}")
            return None

        allowed = [c for c in self.char_to_drink if c.isalpha()]
        not_letters = [c for c in self.char_to_drink if not c.isalpha()]
        if not_letters:
            print(f"{bcolors.WARNING}Only letters can be recognised locally, ignoring drink characters: {not_letters}{bcolors.ENDC}")
        try:
            classifier = LetterClassifier()
        except FileNotFoundError as e:
            print(f"{bcolors.FAIL}{e}{bcolors.ENDC}")
            return None

        print("Recognising the scan locally...")
        pages = analyze_scan(self.scanned_file_path, self.sheet_pages(), allowed, classifier)

        output_pdf_path = os.path.join(self.preferences["program_files_folder"], "annotated_scan.pdf")
        os.makedirs(self.preferences["program_files_folder"], exist_ok=True)
        annotate_pdf(pages, output_pdf_path)
        print(f"PDF with recognised characters saved to {output_pdf_path}")
        self.open_file(output_pdf_path)
        self.scan_pages = pages

        entries = []
        for page in pages:
            page_no = page.index + 1
            if not page.rows:
                print(f"{bcolors.WARNING}Page {page_no}: no names from the list found, page skipped.{bcolors.ENDC}")
            for row in page.rows:
                tokens = []
                for g in row.glyphs:
                    if g.crossed_out:
                        tokens.append(f"crossed out (page {page_no})")
                    elif g.char:
                        tokens.append(g.char)
                    else:
                        tokens.append(f"{g.guess}? {g.confidence:.0%} (page {page_no})")
                entries.append((row.name, tokens, list(row.glyphs)))
                if DEBUG:
                    source = "inferred from position" if row.name_inferred else f"OCR {row.name_score:.2f}"
                    print(f"  page {page_no}: {row.name} ({source}): {tokens}")
            for g in page.stray_glyphs:
                x0, y0, x1, y1 = g.bbox
                print(f"{bcolors.FAIL}Page {page_no}: handwriting outside of any row at x={x0}, y={y0} - "
                      f"check the annotated PDF and add it by hand.{bcolors.ENDC}")
        return entries

    def generate_json_with_payments(self):
        if self.scanned_file_path:
            entries = self.read_scan_azure() if OCR_ENGINE == "azure" else self.read_scan_local()
            if entries is None:
                return

            # Build price lookup from drinks config
            drink_prices = {drink["id"]: drink["price"] for drink in self.drinks}

            # Initialize payment storage
            payments_json = {}
            total_sold = {drink["id"]: 0 for drink in self.drinks}
            total_earnings = 0
            total_unmatched = 0
            # Unmatched characters in the order they appear on the scan (page, row, left to right),
            # so the manual check walks through the paper instead of jumping between pages
            unresolved = []

            # First parse all the data and aggregate by person
            for name, drink_text, glyphs in entries:
                if DEBUG:
                    print(f"\n{bcolors.OKCYAN}Processing: name='{name}', drinks={drink_text}{bcolors.ENDC}")

                amounts = {drink["id"]: 0 for drink in self.drinks}
                unmatched_amount = 0
                unmatched_chars = []
                unmatched_glyphs = []
                for char, glyph in zip(drink_text, glyphs):
                    drink_id = self.drink_for_char(char)
                    if drink_id:
                        amounts[drink_id] += 1
                        if glyph:
                            glyph.final, glyph.final_source = self.drink_char(drink_id), "auto"
                    elif char.strip() != "":
                        unmatched_amount += 1
                        unmatched_chars.append(char)
                        unmatched_glyphs.append(glyph)
                        print(f"Unmatched character found in the scanned text. {bcolors.WARNING}{char}{bcolors.ENDC}")

                person_earnings = 0
                for drink_id, count in amounts.items():
                    total_sold[drink_id] += count
                    person_earnings += count * drink_prices[drink_id]
                total_earnings += person_earnings
                total_unmatched += unmatched_amount

                # Match against loaded persons list, or use OCR name directly
                person_key = name
                for person in self.persons:
                    if unidecode(person['name']).lower() == name.lower():
                        person_key = person['name']
                        break

                if person_key in payments_json:
                    for drink_id in amounts:
                        payments_json[person_key]['drinks'][drink_id] += amounts[drink_id]
                    payments_json[person_key]['unmatched'] += unmatched_amount
                    payments_json[person_key]['unmatched_chars'] += unmatched_chars
                else:
                    payments_json[person_key] = {
                        'drinks': dict(amounts),
                        'unmatched': unmatched_amount,
                        'unmatched_chars': list(unmatched_chars)
                    }
                unresolved.extend((person_key, char, glyph) for char, glyph in zip(unmatched_chars, unmatched_glyphs))

            
            # Update the UI with total values
            for drink in self.drinks:
                if drink["id"] in self.drink_sold_values:
                    self.drink_sold_values[drink["id"]].configure(text=str(total_sold[drink["id"]]))
            self.total_earnings_value.configure(text=str(total_earnings))
            self.total_unmatched_value.configure(text=str(total_unmatched))


            # Resolve unmatched characters before generating messages
            if unresolved:
                print(f"\n{bcolors.FAIL}{bcolors.BOLD}=== UNRESOLVED CHARACTERS ==={bcolors.ENDC}")
                print(f"The following people have unrecognised characters that need to be resolved.")
                print(f"Available drinks:")
                for drink in self.drinks:
                    chars = ", ".join(drink["characters"])
                    print(f"  {bcolors.OKCYAN}{drink['id']}{bcolors.ENDC} - {drink['name']} (characters: {chars})")
                print()

                for number, (person_name, char, glyph) in enumerate(unresolved, start=1):
                    data = payments_json[person_name]
                    # Local OCR describes uncertain glyphs as "T? 62% (page 3)": offer the guess as default
                    if char.startswith("crossed out"):
                        default = "skip"
                    elif len(char) > 1 and char[1] == "?":
                        default = self.drink_for_char(char[0]) or ""
                    else:
                        default = ""
                    hint = f", Enter = {default}" if default else ""
                    while True:
                        print(f"[{number}/{len(unresolved)}] {bcolors.WARNING}{person_name}{bcolors.ENDC}: unrecognised character '{bcolors.BOLD}{char}{bcolors.ENDC}'")
                        choice = input(f"  Assign to which drink ID? (or 'skip' to ignore{hint}): ").strip() or default
                        if choice == "skip":
                            data['unmatched'] -= 1
                            total_unmatched -= 1
                            if glyph:
                                glyph.final, glyph.final_source = None, "skipped"
                            break
                        elif choice in drink_prices:
                            data['drinks'][choice] = data['drinks'].get(choice, 0) + 1
                            total_sold[choice] = total_sold.get(choice, 0) + 1
                            data['unmatched'] -= 1
                            total_unmatched -= 1
                            total_earnings += drink_prices[choice]
                            if glyph:
                                glyph.final, glyph.final_source = self.drink_char(choice), "operator"
                            break
                        else:
                            print(f"  {bcolors.FAIL}Unknown drink ID. Try again.{bcolors.ENDC}")

                for data in payments_json.values():
                    data['unmatched_chars'] = []

                # Update UI after resolution
                for drink in self.drinks:
                    if drink["id"] in self.drink_sold_values:
                        self.drink_sold_values[drink["id"]].configure(text=str(total_sold[drink["id"]]))
                self.total_earnings_value.configure(text=str(total_earnings))
                self.total_unmatched_value.configure(text=str(total_unmatched))
                print(f"\n{bcolors.OKGREEN}All characters resolved. Generating messages...{bcolors.ENDC}")

            # PDF with the final characters of every row, after the operator's decisions
            if self.scan_pages:
                final_pdf_path = os.path.join(self.preferences["program_files_folder"], "final_scan.pdf")
                final_pdf(self.scan_pages, final_pdf_path)
                print(f"PDF with final characters saved to {final_pdf_path}")
                self.open_file(final_pdf_path)

            # Generate JSON
            json_output = []
            drink_name_lookup = {drink["id"]: drink["name"] for drink in self.drinks}
            for person_name, data in payments_json.items():
                person_drinks = data['drinks']
                unmatched_amount = data['unmatched']
                total_chars = sum(person_drinks.values()) + unmatched_amount
                total_price = sum(person_drinks[d_id] * drink_prices[d_id] for d_id in person_drinks)

                if total_chars == 0:
                    continue

                # Print per-person summary
                print(f"\n{bcolors.HEADER}{bcolors.BOLD}Name: {person_name}{bcolors.ENDC}")
                for d_id, count in person_drinks.items():
                    print(f"{bcolors.OKCYAN}{drink_name_lookup[d_id]} Amount: {count}{bcolors.ENDC}")
                print(f"{bcolors.WARNING}Unmatched Amount: {unmatched_amount}{bcolors.ENDC}")

                qr_code_url = "No bank account configured"
                if total_price > 0 and self.preferences.get("bank_account", " ") != " ":
                    try:
                        bank_account_parts = self.preferences["bank_account"].strip().split("/")
                        bank_account = ''.join(c for c in bank_account_parts[0].strip() if c.isdigit())
                        bank_code = ''.join(c for c in bank_account_parts[1].strip()) if len(bank_account_parts) > 1 else ""
                        event_name = self.preferences.get("event_name", "")
                        qr_message = f"{event_name} - {person_name}" if event_name else person_name
                        qr_code_url = generate_czech_qr_code(
                            self.preferences["qr_code_server_url"],
                            account_number=bank_account,
                            bank_code=bank_code,
                            amount=total_price,
                            message=qr_message,
                            size=200
                        )
                    except Exception as e:
                        print(f"Error generating QR code: {e}")

                # Build facts dynamically
                facts = []
                for d_id, count in person_drinks.items():
                    cost = count * drink_prices[d_id]
                    facts.append({"title": f"Počet {drink_name_lookup[d_id]}:", "value": str(count)})
                    facts.append({"title": f"Celková cena za {drink_name_lookup[d_id]}:", "value": f"{cost} Kč"})
                facts.append({"title": "Celková cena za vše:", "value": f"{total_price} Kč"})

                json_output.append({
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": [
                        {"type": "TextBlock", "text": f"Shrnutí pro {person_name}", "size": "Medium", "weight": "Bolder"},
                        {"type": "FactSet", "facts": facts},
                        {"type": "Image", "url": qr_code_url, "size": "Medium"}
                    ]
                })

            # Save JSON to file
            json_file_path = os.path.join(self.preferences["program_files_folder"], "payments.json")
            with open(json_file_path, "w", encoding="utf-8") as jsonfile:
                dump(json_output, jsonfile, ensure_ascii=False, indent=4)

            print(f"Payments JSON generated successfully as '{json_file_path}'.")

            # Save CSV summary
            csv_file_path = os.path.join(self.preferences["program_files_folder"], "payments.csv")
            with open(csv_file_path, "w", newline="", encoding="utf-8") as csvfile:
                writer = csv.writer(csvfile, delimiter=";")
                writer.writerow(["name", "total_amount"])
                for person_name, data in payments_json.items():
                    total = sum(data['drinks'][d_id] * drink_prices[d_id] for d_id in data['drinks'])
                    if total > 0:
                        writer.writerow([person_name, total])
            print(f"CSV summary saved as '{csv_file_path}'.")

            # Generate messages for participants
            message_template = self.preferences.get("message_template", "")
            if message_template:
                # Cut-outs of each person's rows from the final scan, embedded in the message so they
                # can check them (Teams Flow bot cannot send file attachments)
                row_images = person_row_images(self.scan_pages) if self.scan_pages else {}
                messages = []
                for person_key, data in payments_json.items():
                    # Persons from CSV are keyed as "Surname First"; use the original "First Surname"
                    person_name = next((p['full_name'] for p in self.persons
                                        if p['name'] == person_key and 'full_name' in p), person_key)
                    person_drinks = data['drinks']
                    total_chars = sum(person_drinks.values())
                    if total_chars == 0:
                        continue

                    total_price = sum(person_drinks[d_id] * drink_prices[d_id] for d_id in person_drinks)
                    drinks_summary = "".join(
                        f"<li>{drink_name_lookup[d_id]}: {count}x ({count * drink_prices[d_id]} Kč)</li>"
                        for d_id, count in person_drinks.items() if count > 0
                    )

                    qr_url = ""
                    if total_price > 0 and self.preferences.get("bank_account", " ") != " ":
                        try:
                            bank_account_parts = self.preferences["bank_account"].strip().split("/")
                            acc = ''.join(c for c in bank_account_parts[0].strip() if c.isdigit())
                            code = ''.join(c for c in bank_account_parts[1].strip()) if len(bank_account_parts) > 1 else ""
                            event_name = self.preferences.get("event_name", "")
                            qr_message = f"{event_name} - {person_name}" if event_name else person_name
                            qr_url = generate_czech_qr_code(
                                self.preferences["qr_code_server_url"],
                                account_number=acc,
                                bank_code=code,
                                amount=total_price,
                                message=qr_message,
                                size=200
                            )
                        except Exception:
                            qr_url = ""

                    qr_html = f'<img src="{qr_url}" alt="QR platba">' if qr_url else ""

                    template_values = dict(
                        name=person_name,
                        event_name=self.preferences.get("event_name", ""),
                        organizer_name=self.preferences.get("organizer_name", ""),
                        drinks_summary=drinks_summary,
                        total_price=total_price,
                        bank_account=self.preferences.get("bank_account", ""),
                        qr_code_url=qr_html,
                        rows_image=""
                    )
                    message = message_template.format(**template_values)

                    # Inline image of the person's rows: at {rows_image} in the template, or appended.
                    # JPEG quality/size is lowered until the whole message fits the Teams limit.
                    image = row_images.get(unidecode(person_key))
                    if image is not None:
                        prefix = 'Tvoje řádky ze seznamu:<br><img src="data:image/jpeg;base64,'
                        suffix = '" alt="Tvoje řádky ze seznamu">'
                        separator = "" if "{rows_image}" in message_template else "<br><br>"
                        fixed = len((message + separator + prefix + suffix).encode("utf-8"))
                        jpg = encode_jpeg_within(image, (TEAMS_MESSAGE_MAX_BYTES - fixed) * 3 // 4)
                        if jpg:
                            rows_html = prefix + base64.b64encode(jpg).decode("ascii") + suffix
                            if "{rows_image}" in message_template:
                                message = message_template.format(**{**template_values, "rows_image": rows_html})
                            else:
                                message += separator + rows_html
                        else:
                            print(f"{bcolors.WARNING}{person_name}: rows image does not fit into the message, "
                                  f"sent without it.{bcolors.ENDC}")
                    # Prefer the email from the CSV; otherwise build "first.surname" from the key
                    # "Surname First [Middle]": "Novák Jan" → "jan.novak@studentstc.cz" (middle names ignored)
                    email = next((p.get('email') for p in self.persons
                                  if p['name'] == person_key and p.get('email')), "")
                    if not email:
                        name_parts = unidecode(person_key).lower().split()
                        if len(name_parts) > 1:
                            email = f"{name_parts[1]}.{name_parts[0]}@studentstc.cz"
                        else:
                            email = f"{name_parts[0]}@studentstc.cz"

                    messages.append({"email": email, "message": message})

                messages_file_path = os.path.join(self.preferences["program_files_folder"], "messages.json")
                with open(messages_file_path, "w", encoding="utf-8") as f:
                    dump(messages, f, ensure_ascii=False, indent=4)
                print(f"Messages generated successfully as '{messages_file_path}'.")

            # Print totals
            print(f"\n{bcolors.HEADER}=== OVERVIEW ==={bcolors.ENDC}")
            for drink in self.drinks:
                print(f"{bcolors.OKCYAN}Total {drink['name']} Sold: {total_sold[drink['id']]}{bcolors.ENDC}")
            print(f"{bcolors.OKCYAN}Earning: {total_earnings}{bcolors.ENDC}")
            print(f"{bcolors.WARNING}Unmatched Amount: {total_unmatched}{bcolors.ENDC}")

            # List people with unrecognised characters
            people_with_unmatched = [name for name, data in payments_json.items() if data['unmatched'] > 0]
            if people_with_unmatched:
                print(f"\n{bcolors.FAIL}{bcolors.BOLD}People with unrecognised characters:{bcolors.ENDC}")
                for name in people_with_unmatched:
                    print(f"  {bcolors.WARNING}- {name} ({payments_json[name]['unmatched']} unrecognised){bcolors.ENDC}")
        else:
            print("No scanned file selected.")

if __name__ == "__main__":
    app = PayMeADrink()