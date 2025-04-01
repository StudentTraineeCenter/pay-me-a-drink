import customtkinter as ctk
from customtkinter import filedialog
import csv
from reportlab.pdfgen import canvas
from unidecode import unidecode
import os
from azure.cognitiveservices.vision.computervision import ComputerVisionClient
from msrest.authentication import CognitiveServicesCredentials
import time
import re
import json
from urllib.parse import urlencode
import smtplib
from dotenv import load_dotenv
import pprint
from PyPDF2 import PdfReader, PdfWriter
from io import BytesIO
from json import dump  # Add import for JSON dumping
# Add new imports
from pdf2image import convert_from_path
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import letter
import fitz  # PyMuPDF

max_persons_per_page = 18
PREFERENCES_FILE = "preferences.json"
program_files_folder_path = "program_files"

PAYMENTS_JSON = {}

DEBUG = True

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


def load_preferences():
    if os.path.exists(PREFERENCES_FILE):
        with open(PREFERENCES_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    return {"coke_price": 20, "beer_price": 30, "bank_account": " "}

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

def contains_only_drink_characters(text):
    """
    Check if a text contains only characters used for drink tracking (K, k, P).
    
    Args:
        text: The text to check
    
    Returns:
        bool: True if the text contains only relevant drink characters, False otherwise
    """
    # Strip whitespace before checking
    clean_text = text.strip()
    if not clean_text:
        return False
        
    # Check if all characters are in the allowed set
    drink_chars = set(" KkP")
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


def load_credentials():
    load_dotenv()

    global AZURE_ENDPOINT, AZURE_API_KEY
    
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


class PayMeADrink:
    def __init__(self):
        load_credentials()  # Load credentials at the start
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
        
        self.label_second = ctk.CTkLabel(self.right_frame, text="Poslání QR plateb.", font=("Helvetica", 16, "bold"))
        self.label_second.pack(pady=10)
        
        self.file_button_scan = ctk.CTkButton(self.right_frame, text="Vybrat sken papíru", command=self.browse_file_scan)
        self.file_button_scan.pack(side="left", padx=10)

        self.generate_json_with_payments = ctk.CTkButton(self.right_frame, text="Vygenerovat JSON s platbami", command=self.generate_json_with_payments)
        self.generate_json_with_payments.pack(side="left", padx=10)
        
        self.settings_label = ctk.CTkLabel(self.settings_frame, text="Nastavení cen a účtu", font=("Helvetica", 16, "bold"))
        self.settings_label.pack(pady=10)
        
        self.preferences = load_preferences()

        self.coke_price_frame = ctk.CTkFrame(self.settings_frame)
        self.coke_price_frame.pack(pady=5, fill="x")
        self.coke_price_label = ctk.CTkLabel(self.coke_price_frame, text="Cena za Kofolu:              ")
        self.coke_price_label.pack(side="left", padx=5)
        self.coke_price_entry = ctk.CTkEntry(self.coke_price_frame)
        self.coke_price_entry.insert(0, str(self.preferences["coke_price"]))
        self.coke_price_entry.pack(side="left", padx=5)

        self.beer_price_frame = ctk.CTkFrame(self.settings_frame)
        self.beer_price_frame.pack(pady=5, fill="x")
        self.beer_price_label = ctk.CTkLabel(self.beer_price_frame, text="Cena za Pivo:                  ")
        self.beer_price_label.pack(side="left", padx=5)
        self.beer_price_entry = ctk.CTkEntry(self.beer_price_frame)
        self.beer_price_entry.insert(0, str(self.preferences["beer_price"]))
        self.beer_price_entry.pack(side="left", padx=5)

        self.bank_account_frame = ctk.CTkFrame(self.settings_frame)
        self.bank_account_frame.pack(pady=5, fill="x")
        self.bank_account_label = ctk.CTkLabel(self.bank_account_frame, text="Číslo bankovního účtu:")
        self.bank_account_label.pack(side="left", padx=5)
        self.bank_account_entry = ctk.CTkEntry(self.bank_account_frame)
        self.bank_account_entry.insert(0, self.preferences["bank_account"])
        self.bank_account_entry.pack(side="left", padx=5)

        self.save_button = ctk.CTkButton(self.settings_frame, text="Uložit ceny a účet", command=self.save_preferences)
        self.save_button.pack(pady=10)

        self.earnings_label = ctk.CTkLabel(self.earnings_frame, text="Tržby", font=("Helvetica", 16, "bold"))
        self.earnings_label.pack(pady=10)

        self.total_coke_frame = ctk.CTkFrame(self.earnings_frame)
        self.total_coke_frame.pack(pady=5, fill="x")
        self.total_coke_label = ctk.CTkLabel(self.total_coke_frame, text="Celkem Kofol prodáno:   ")
        self.total_coke_label.pack(side="left", padx=5)
        self.total_coke_value = ctk.CTkLabel(self.total_coke_frame, text="0")
        self.total_coke_value.pack(side="left", padx=5)
    
        self.total_beer_frame = ctk.CTkFrame(self.earnings_frame)
        self.total_beer_frame.pack(pady=5, fill="x")
        self.total_beer_label = ctk.CTkLabel(self.total_beer_frame, text="Celkem Piv prodáno: ")
        self.total_beer_label.pack(side="left", padx=5)
        self.total_beer_value = ctk.CTkLabel(self.total_beer_frame, text="0")
        self.total_beer_value.pack(side="left", padx=5)

        self.total_earnings_frame = ctk.CTkFrame(self.earnings_frame)
        self.total_earnings_frame.pack(pady=5, fill="x")
        self.total_earnings_label = ctk.CTkLabel(self.total_earnings_frame, text="Celkový výdělek:      ")
        self.total_earnings_label.pack(side="left", padx=5)
        self.total_earnings_value = ctk.CTkLabel(self.total_earnings_frame, text="0")
        self.total_earnings_value.pack(side="left", padx=5)

        self.total_unmatched_frame = ctk.CTkFrame(self.earnings_frame)
        self.total_unmatched_frame.pack(pady=5, fill="x")
        self.total_unmatched_label = ctk.CTkLabel(self.total_unmatched_frame, text="Neuznané znaky: ")
        self.total_unmatched_label.pack(side="left", padx=5)
        self.total_unmatched_value = ctk.CTkLabel(self.total_unmatched_frame, text="0")
        self.total_unmatched_value.pack(side="left", padx=5)
        
        self.persons = []
        
        self.scanned_file_path = None
        
        self.app.mainloop()

    def browse_file(self):
        file_path = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv")])
        if file_path:
            filename = os.path.basename(file_path)
            self.file_button.configure(text=filename)
            
            with open(file_path, newline='', encoding='utf-8') as csvfile:
                reader = csv.reader(csvfile, delimiter=';')
                for row in reader:
                    if len(row) >= 2:
                        self.persons.append({'name': row[0], 'email': row[1]})

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
        if not self.persons:
            print("No data available to generate PDF.")
            return
        
        # Make sure the directory exists
        os.makedirs(program_files_folder_path, exist_ok=True)
        
        file_name = os.path.join(program_files_folder_path, "pivni_seznam.pdf")
        c = canvas.Canvas(file_name)
        
        page_number = 1
        y_position = 750  # Starting y position on the page
        
        
        duplicated_persons = []
        for person in self.persons:
            for i in range(2):
                duplicated_persons.append(person)
        
        black_color = (0, 0, 0)
        grey_color = (0.4, 0.4, 0.4)
        
        
        for index, person in enumerate(duplicated_persons, start=1):   
            c.setFont("Helvetica-Bold", 20)            
            # Determine color based on the index (change color every 2 names)
            if (index - 1) // 2 % 2 == 0:  # Use integer division to change color every 2 people (4 entries in duplicated list)
                c.setFillColorRGB(*black_color)
            else:
                c.setFillColorRGB(*grey_color)
            
            max_number_of_underscores = 50
            c.drawString(20, y_position-3, "_" * max_number_of_underscores)
            c.drawString(20, y_position, f"@{unidecode(person['name'])}@")
            y_position -= 40  # Move down by 30 units for the next entry

            if index % max_persons_per_page == 0 or index == len(duplicated_persons):
                # Add footer with page number
                c.setFillColorRGB(*black_color)
                c.drawString(265, 20, f"Strana {page_number}")
                page_number += 1
                c.showPage()  # Create a new page
                y_position = 750  # Reset y position for the new page

        c.save()
        print(f"PDF generated successfully as '{file_name}'.")
        
        # Open the generated PDF
        os.startfile(file_name)
        
    def save_preferences(self):
        self.preferences["coke_price"] = int(self.coke_price_entry.get())
        self.preferences["beer_price"] = int(self.beer_price_entry.get())
        self.preferences["bank_account"] = self.bank_account_entry.get()
        save_preferences(self.preferences)
        print("Preferences saved:", self.preferences)
     
    def generate_json_with_payments(self):
        if self.scanned_file_path:
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

            if DEBUG:
                # Create annotated PDF with bounding boxes
                output_pdf_path = os.path.join(program_files_folder_path, "annotated_scan.pdf")
                os.makedirs(program_files_folder_path, exist_ok=True)
                # Pass the persons list to the function
                annotated_pdf = draw_bounding_boxes_on_pdf(
                    self.scanned_file_path, 
                    output_pdf_path, 
                    all_bounding_boxes,
                    self.persons
                )
                
                # Open the annotated PDF
                os.startfile(annotated_pdf)

            # Continue with existing processing
            final_text = final_text.replace(" ", "").replace("-", "").replace("_", "").replace("Strana", "")
            final_text = ''.join([i for i in final_text if not i.isdigit()])
            final_text = final_text.split("@")[1:]

            coke_price = self.preferences["coke_price"]
            beer_price = self.preferences["beer_price"]

            # Initialize payment storage
            payments_json = {}
            total_coke_sold = 0
            total_beer_sold = 0
            total_earnings = 0
            total_unmatched = 0

            # First parse all the data and aggregate by person
            for i in range(0, len(final_text), 2):
                if i + 1 >= len(final_text):
                    break

                name = re.findall(r'[A-Z][a-z]*', final_text[i])
                name = " ".join(name)
                drinks = final_text[i + 1]

                coke_amount = 0
                beer_amount = 0
                unmatched_amount = 0  # Reset for each person
                for char in drinks:
                    if char == "K" or char == "k":
                        coke_amount += 1
                    elif char == "P":
                        beer_amount += 1
                    else:
                        if char.strip() != "":
                            unmatched_amount += 1  # Increment unmatched amount
                            print(f"Unmatched character found in the scanned text. {bcolors.WARNING}{char}{bcolors.ENDC}")

                total_coke_sold += coke_amount
                total_beer_sold += beer_amount
                total_earnings += coke_amount * coke_price + beer_amount * beer_price
                total_unmatched += unmatched_amount  # Accumulate unmatched amount globally

                # Find the person in the persons list
                matched_person = None
                for person in self.persons:
                    if unidecode(person['name']).lower() == name.lower():
                        matched_person = person
                        break

                if matched_person:
                    person_key = matched_person['name']
                    if person_key in payments_json:
                        payments_json[person_key]['coke'] += coke_amount
                        payments_json[person_key]['beer'] += beer_amount
                        payments_json[person_key]['unmatched'] += unmatched_amount
                    else:
                        payments_json[person_key] = {
                            'email': matched_person['email'],
                            'coke': coke_amount,
                            'beer': beer_amount,
                            'unmatched': unmatched_amount
                        }
                else:
                    print(f"{bcolors.WARNING}{bcolors.BOLD}No match found for name: {name}{bcolors.ENDC}")

            
            # Update the UI with total values
            self.total_coke_value.configure(text=str(total_coke_sold))
            self.total_beer_value.configure(text=str(total_beer_sold))
            self.total_earnings_value.configure(text=str(total_earnings))
            self.total_unmatched_value.configure(text=str(total_unmatched))
            
            #Print it
            print(f"""
{bcolors.OKCYAN}Total Coke Sold: {total_coke_sold}{bcolors.ENDC}
{bcolors.OKCYAN}Total Beer Sold: {total_beer_sold}{bcolors.ENDC}
{bcolors.OKCYAN}Earning: {total_earnings}{bcolors.ENDC}
{bcolors.WARNING}Unmatched Amount: {total_unmatched}{bcolors.ENDC}
    """)
            # Generate JSON
            json_output = []
            for person_name, data in payments_json.items():
                

                
                coke_amount = data['coke']
                beer_amount = data['beer']
                coke_cost = coke_amount * coke_price
                beer_cost = beer_amount * beer_price
                total_price = coke_cost + beer_cost
                unmatched_amount = data['unmatched']
                
                
                                                # Print unmatched amount for the current person
                print(f"""
{bcolors.HEADER}{bcolors.BOLD}Name: {person_name}{bcolors.ENDC}
{bcolors.OKCYAN}Coke Amount: {coke_amount}{bcolors.ENDC}
{bcolors.OKCYAN}Beer Amount: {beer_amount}{bcolors.ENDC}
{bcolors.WARNING}Unmatched Amount: {unmatched_amount}{bcolors.ENDC}
    """)
                
                qr_code_url = "No bank account configured"
                if total_price > 0 and self.preferences["bank_account"] != " ":
                    try:
                        bank_account_parts = self.preferences["bank_account"].strip().split("/")
                        bank_account = ''.join(c for c in bank_account_parts[0].strip() if c.isdigit())
                        bank_code = ''.join(c for c in bank_account_parts[1].strip()) if len(bank_account_parts) > 1 else ""
                        qr_code_url = generate_czech_qr_code(
                            "https://api.paylibo.com/paylibo",
                            account_number=bank_account,
                            bank_code=bank_code,
                            amount=total_price,
                            message=f"Platba za nápoje: Kofola: {coke_amount}x Pivo: {beer_amount}x",
                            size=200
                        )
                    except Exception as e:
                        print(f"Error generating QR code: {e}")

                json_output.append({
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": [
                        {"type": "TextBlock", "text": f"Shrnutí pro {person_name}", "size": "Medium", "weight": "Bolder"},
                        {"type": "TextBlock", "text": f"Email: {data['email']}", "wrap": True},
                        {"type": "FactSet", "facts": [
                            {"title": "Počet Kofol:", "value": str(coke_amount)},
                            {"title": "Celková cena za Kofoly", "value": f"{coke_cost} Kč"},
                            {"title": "Počet piv:", "value": str(beer_amount)},
                            {"title": "Celková cena za piva:", "value": f"{beer_cost} Kč"},
                            {"title": "Celková cena za vše:", "value": f"{total_price} Kč"}
                        ]},
                        {"type": "Image", "url": qr_code_url, "size": "Medium"}
                    ]
                })

            # Save JSON to file
            json_file_path = os.path.join(program_files_folder_path, "payments.json")
            with open(json_file_path, "w", encoding="utf-8") as jsonfile:
                dump(json_output, jsonfile, ensure_ascii=False, indent=4)

            print(f"Payments JSON generated successfully as '{json_file_path}'.")
        else:
            print("No scanned file selected.")

if __name__ == "__main__":
    app = PayMeADrink()