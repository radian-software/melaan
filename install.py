with open("controller.py") as f:
    code = f.read()

print(
    f"""
with open('main.py', 'w') as f:
    f.write({repr(code)})
    """
)
