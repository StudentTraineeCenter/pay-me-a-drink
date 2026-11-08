# Pay me a drink

Application for generating Adaptive Cards Json to people who owe you a drink. The application uses Azure Text Analytics to analyze the text of the email and determine if the person write a character in the drink list. If the person writes a character in the drink list, the application creates Adaptive Cards Json that can be used to send emails to the person with the list of people who owe him a drink.

## Installation

1. Clone the repository
2. Create a virtual environment and activate it
   1. `python -m venv venv`
   2. `venv\Scripts\activate`
   3. `pip install -r requirements.txt`
3. Create an Azure account and create a Computer Vision resource. You can find the instructions [here](https://learn.microsoft.com/en-us/azure/cognitive-services/computer-vision/quickstarts-sdk/python-sdk).
4. Create a `.env` file in the root directory of your project with the following content:
   ```
   # .env
   AZURE_ENDPOINT=""
   AZURE_API_KEY=""
   ```

## Dependencies

- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) for reading the printed names (`sudo apt install tesseract-ocr`, Windows: [installer](https://github.com/UB-Mannheim/tesseract/wiki))
- Letter model `models/letter_cnn.pt` (see below)
- Optional: [Computer vision](https://portal.azure.com/#create/Microsoft.CognitiveServicesComputerVision) when `OCR_ENGINE=azure` is set in `.env`

## Recognition of the scan

By default the scan is recognised locally (`handwriting_ocr.py`), no cloud service is needed:

1. Blue pen ink is separated from the printed sheet by colour, so other papers, the table or printed text never count as drinks.
2. Printed underlines define the rows; the printed names are read by Tesseract and matched against the loaded CSV. Rows Tesseract cannot read get their name from their position on the sheet.
3. Each handwritten character is classified by a small neural network that only chooses between the drink characters.
4. Uncertain or crossed-out characters are not guessed silently: they are offered for manual resolution in the terminal (Enter accepts the suggestion). Handwriting outside any row is reported.

`program_files/annotated_scan.pdf` shows the result: green = accepted, red = needs confirmation, grey = crossed out, orange = outside of any row.

Please ask people to write with a **blue pen** - black ink cannot be told apart from the printed sheet.

### Training the letter model

The model is trained once on the EMNIST handwritten letters (~560 MB download, training takes ~10 minutes on CPU):

```
curl -L -o .cache/emnist_gzip.zip https://biometrics.nist.gov/cs_links/EMNIST/gzip.zip
unzip -j .cache/emnist_gzip.zip 'gzip/emnist-letters-*' -d .cache/emnist
python train_letter_model.py
```

## How to use

Run the application with the command `python app.py`.
The application will open a GUI where you can select the file with the list of people who owe you a drink and the file with the drink list. The application will then generate a JSON file with the payments for each person who owes you a drink.

1. Create a list of people who owe you a drink in the `people.csv` file.
2. Run the application with the command `python app.py`.
3. Click button `Prozkoumat` and select the file `people.csv`.
4. Then click button `Vygenerovat PDF`.
5. For now you can close the application.
6. Fill out the drink list and scan the document and save it as `drink_list.pdf`.
7. Run the application with the command `python app.py`.
8. Do the same as in step 3 to load the user list so the program can match the names.
9. Fill out the prices for the drinks and the bank account number in format `4567890/1234`.
10. Click `Uložit ceny a účet`.
11. Click 'Vybrat sken papíru' and select the `drink_list.pdf` file.
12. Click `Vygenerovat JSON s platbami`.
13. In the terminal check the output if there where any unrecognized names or characters.

### Information

- Structure of input csv file:

  ```
  Jan Novák;jan.novak@studentstc.cz
  ```

  The name is written as "First [Middle] Surname". The sheet is generated and checked as `@Novak Jan@` (surname first, without diacritics); messages use the original "Jan Novák" and the email from the CSV.

- Recognizable characters in the drink list: `K`, `k`, `P`, `p`.
