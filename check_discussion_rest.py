#!/usr/bin/env python3
import requests
import os

token = os.environ.get('GITHUB_TOKEN')
headers = {
    'Authorization': f'Bearer {token}',
    'Accept': 'application/vnd.github.v3+json'
}

# Get discussion #20 from tinkermonkey/documentation_robotics
response = requests.get(
    'https://api.github.com/repos/tinkermonkey/documentation_robotics/discussions/20',
    headers=headers
)

if response.status_code == 200:
    data = response.json()
    print(f"Discussion #{data['number']}: {data['title']}")
    print(f"Created: {data['created_at']}")
    print(f"Comments: {data.get('comments', 'N/A')}")
else:
    print(f"Error: {response.status_code}")
    print(response.text)
