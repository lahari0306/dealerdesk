
import requests

for i in range(2):
    r = requests.post(
        "http://127.0.0.1:8000/api/sync/supplier/",
        timeout=150
    )
    print(r.status_code, r.json())
