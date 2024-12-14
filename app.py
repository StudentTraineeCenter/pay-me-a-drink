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

max_persons_per_page = 18
PREFERENCES_FILE = "preferences.json"

def load_preferences():
    if os.path.exists(PREFERENCES_FILE):
        with open(PREFERENCES_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    return {"coke_price": 20, "beer_price": 30, "bank_account": " "}

def save_preferences(preferences):
    with open(PREFERENCES_FILE, "w", encoding="utf-8") as file:
        json.dump(preferences, file)


def load_credentials():
    load_dotenv()

    global AZURE_ENDPOINT, AZURE_API_KEY, GMAIL_MY_ADDRESS, GMAIL_PASSWORD
    
    AZURE_ENDPOINT = os.getenv('AZURE_ENDPOINT')
    AZURE_API_KEY = os.getenv('AZURE_API_KEY')
    GMAIL_MY_ADDRESS = os.getenv('GMAIL_MY_ADDRESS')
    GMAIL_PASSWORD = os.getenv('GMAIL_PASSWORD')

def send_gmail(sent_to, sent_body):
    # =============================================================================
    # SET EMAIL LOGIN REQUIREMENTS
    # =============================================================================
    gmail_user = GMAIL_MY_ADDRESS
    gmail_app_password = GMAIL_PASSWORD

    # =============================================================================
    # SET THE INFO ABOUT THE SAID EMAIL
    # =============================================================================
    sent_from = gmail_user
    sent_subject = "Pay me a drink"
    
    email_text = f"""\
From: {sent_from}
To: {", ".join(sent_to)}
Subject: {sent_subject}

{sent_body}
    """

    # =============================================================================
    # SEND EMAIL OR DIE TRYING!!!
    # Details: http://www.samlogic.net/articles/smtp-commands-reference.htm
    # =============================================================================



    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.ehlo()
        server.login(gmail_user, gmail_app_password)
        server.sendmail(sent_from, sent_to, email_text.encode('utf-8'))
        server.close()

        print('Email sent!')
    except Exception as exception:
        print("Error: %s!\n\n" % exception)


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
        self.earnings_frame.pack(side="left", padx=10, pady=10, fill="x")    

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

        self.generate_csv_with_payments = ctk.CTkButton(self.right_frame, text="Vygenerovat CSV s platbami", command=self.generate_csv_with_payments)
        self.generate_csv_with_payments.pack(side="left", padx=10)
        
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
        
        
        self.send_frame = ctk.CTkFrame(self.bottom_frame)
        self.send_frame.pack(pady=5, fill="x", side="right")
        
        
        self.send_email_button = ctk.CTkButton(self.send_frame, text="Poslat emaily s QR kódy.", command=self.send_email)
        self.send_email_button.pack(pady=10)
        
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
                self.persons = [{'name': row[0], 'email': row[1]} for row in reader]
                print("Loaded data:", self.persons)
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

        file_name = "pivni_seznam.pdf"
        c = canvas.Canvas(file_name)
        
        page_number = 1
        y_position = 750  # Starting y position on the page
        
        for index, person in enumerate(self.persons, start=1):
            c.setFont("Helvetica-Bold", 20)
            max_number_of_underscores = 50
            c.drawString(20, y_position-3, "_" * max_number_of_underscores)
            c.drawString(20, y_position, f"@{unidecode(person['name'])}@")
            y_position -= 40  # Move down by 30 units for the next entry

            if index % max_persons_per_page == 0 or index == len(self.persons):
                # Add footer with page number
                c.drawString(265, 20, f"Strana {page_number}")
                page_number += 1
                c.showPage()  # Create a new page
                y_position = 750  # Reset y position for the new page

        c.save()
        print("PDF generated successfully as 'generated_report.pdf'.")
        
        #Open the generated PDF
        os.startfile(file_name, "open")
        
    def save_preferences(self):
        self.preferences["coke_price"] = int(self.coke_price_entry.get())
        self.preferences["beer_price"] = int(self.beer_price_entry.get())
        self.preferences["bank_account"] = self.bank_account_entry.get()
        save_preferences(self.preferences)
        print("Preferences saved:", self.preferences)

    def generate_csv_with_payments(self):
        if self.scanned_file_path != None:
            # Replace with your key and endpoint
            api_key = AZURE_API_KEY
            endpoint = AZURE_ENDPOINT
            # Open the image file and send it to the API
            client = ComputerVisionClient(endpoint, CognitiveServicesCredentials(api_key))
            with open(self.scanned_file_path, "rb") as image_stream:
                # Send the image to the API
                response = client.read_in_stream(image_stream, raw=True)

            # Get the operation location (used to track the progress of the read operation)
            operation_location = response.headers["Operation-Location"]
            operation_id = operation_location.split("/")[-1]

            # Poll for the results
            while True:
                result = client.get_read_result(operation_id)
                if result.status not in ["notStarted", "running"]:
                    break
                print("Waiting for result...")
                time.sleep(1)

            # Process and print the results
            final_text = ""
            
            if result.status == "succeeded":
                for page in result.analyze_result.read_results:
                    for line in page.lines:
                        final_text += line.text
            else:
                print("Analysis failed.")
                
            final_text = final_text.replace(" ", "").replace("-", "").replace("_", "").replace("Strana", "")
            
            final_text = ''.join([i for i in final_text if not i.isdigit()])
            
            final_text = final_text.split("@")[1:]
            
            coke_price = self.preferences["coke_price"]
            beer_price = self.preferences["beer_price"]
            
            total_coke_sold = 0
            total_beer_sold = 0
            total_earnings = 0
            total_unmatched = 0
            
            
            with open("payments.csv", "w", newline='', encoding="utf-8") as csvfile:
                for i in range(0, len(final_text), 2):
                    name = re.findall(r'[A-Z][a-z]*', final_text[i])
                    name = " ".join(name)
                    drinks = final_text[i+1]
                    
                    coke_amount =  0
                    beer_amount = 0
                    unmatched_amount = 0
                    for char in drinks:
                        if char == "K":
                            coke_amount += 1
                        elif char == "P":
                            beer_amount += 1
                        else:
                            unmatched_amount += 1
                            print(f"Unmatched character found in the scanned text. {char}")
                    
                    total_coke_sold += coke_amount
                    total_beer_sold += beer_amount
                    total_earnings += coke_amount * coke_price + beer_amount * beer_price
                    total_unmatched += unmatched_amount
                    
                    total_price_per_person = coke_amount * coke_price + beer_amount * beer_price
                    # Find the person in the persons list
                    matched_person = None
                    for person in self.persons:
                        if unidecode(person['name']).lower() == name.lower():
                            matched_person = person
                            break

                    if matched_person:
                        print(f"Matched person: {matched_person['name']}")
                        # Generate QR code for the payment
                        
                        if self.preferences["bank_account"] != " ":
                            bank_account, bank_code =  self.preferences["bank_account"].split("/")
                            
                        
                        
                        qr_code_url = generate_czech_qr_code("https://api.paylibo.com/paylibo", account_number=bank_account, bank_code=bank_code, amount=(total_price_per_person), message=f"Platba za nápoje: Kofola: {coke_amount}x Pivo: {beer_amount}x", size=200)

                        
                        if(total_price_per_person > 0):
                            csvfile.write(f"{matched_person['name']};{matched_person['email']};{coke_amount};{coke_amount*coke_price};{beer_amount};{beer_amount*beer_price};{total_price_per_person};{qr_code_url}\n")
                    else:
                        print(f"No match found for name: {name}")
                    
                    print(f"Name: {name}, Coke: {coke_amount}, Beer: {beer_amount}, Unmatched: {unmatched_amount}, Payment: {total_price_per_person}")
                
            self.total_coke_value.configure(text=str(total_coke_sold))
            self.total_beer_value.configure(text=str(total_beer_sold))
            self.total_earnings_value.configure(text=str(total_earnings))
            self.total_unmatched_value.configure(text=str(total_unmatched))
            
            os.startfile("payments.csv", "open")
                
            
        else:
            print("No scanned file selected.")
            
    def send_email(self):
        with open("payments.csv", newline='', encoding="utf-8") as csvfile:
            reader = csv.reader(csvfile, delimiter=';')
            
            print("Sending emails...")
            for row in reader:
                
                message = f"""Dobrý den,
                {row[0]},
                prosím o zaplacení následující platby:
                Kofola: {row[2]}x za {row[3]} Kč
                Pivo: {row[4]}x za {row[5]} Kč
                Celkem: {row[6]} Kč
                QR kód pro platbu: {row[7]}
                """
                
                email_receiver = []
                email_receiver.append(row[1])
                print(f"Sending email to {email_receiver}")
                
                
                send_gmail(email_receiver, message)
                time.sleep(1)

if __name__ == "__main__":
    app = PayMeADrink()
