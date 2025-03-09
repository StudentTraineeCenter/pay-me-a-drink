### Pay me a drink

Application for sending emails to people who owe you a drink. The application uses Azure Text Analytics to analyze the text of the email and determine if the person write a character in the drink list. If the person writes a character in the drink list, the application sends an email to the person with the list of people who owe him a drink.

To set up the environment variables, create a `.env` file in the root directory of your project with the following content:

```
# .env
AZURE_ENDPOINT=""
AZURE_API_KEY=""
GMAIL_MY_ADDRESS=""
GMAIL_PASSWORD=""
```

Gmail password is an app password, not your real password. You can create one [here](https://myaccount.google.com/apppasswords).

Install the dependencies:

```bash
pip install -r requirements.txt
```

Structure of input csv file:

```
name;email
```

Recognizable characters in the drink list: `K` , `P`.

# Dependencies

- [Computer vision](https://portal.azure.com/#create/Microsoft.CognitiveServicesComputerVision)

# How to use

1. Create a list of people who owe you a drink in the `people.csv` file.
2. Run the application with the command `python app.py`.
3. Click button `Prozkoumat` and select the file `people.csv`.
4. Then click button `Vygenerovat PDF`.
5. For now you can close the application.
6. Fill out the drink list and scan the document and save it as `drink_list.pdf`.
7. Run the application with the command `python app.py`.
8. Do the same as in step 3 to load the user list so the program can matched the names.
9. Fill out the prices for the drinks and the bank account number in format `4567890/1234`.
10. Click `Uložit ceny a účet`.
11. Click 'Vybrat sken papíru' and select the `drink_list.pdf` file.
12. Click `Vygenerovat CSV s platbami`.
13. In the terminal check the output if there where any unrecognized names or characters.
14. If everything is ok, click `Poslat emaily s QR kódy.`.
