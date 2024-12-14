### Pay me a drink

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
