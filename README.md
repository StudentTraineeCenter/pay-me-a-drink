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
