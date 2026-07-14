with open("controller.py") as f:
    code = f.read()

print(f"""
with open('main.py', 'w') as f:
    f.write({repr(code)})
    """)

for filename in (
    "CONTROLLER_PASSWORD",
    "SERVER_ADDRESS",
    "WIFI_CREDENTIALS",
    "melaan-ca.der",
):
    with open(filename, "rb") as f:
        print(f"""
with open('{filename}', 'wb') as f:
    f.write({repr(f.read())})
        """)
