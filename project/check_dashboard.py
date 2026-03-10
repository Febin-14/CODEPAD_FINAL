import requests

response = requests.get('http://localhost:8000/dashboard', cookies={'username': 'manager', 'role': 'manager'})
for line in response.text.splitlines():
    if "delete_project" in line:
        print(line.strip())
