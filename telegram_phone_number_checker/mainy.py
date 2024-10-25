import re
from bs4 import BeautifulSoup

html_name = 'alb2.html'
export = 'phones.txt'


def extract_phone_numbers(html_content):
    phone_pattern = re.compile(r'\+?\d[\d\s\-\(\)]{7,}\d')
    return phone_pattern.findall(html_content)


with open(html_name, 'r', encoding='utf-8') as file:
    html_content = file.read()

soup = BeautifulSoup(html_content, 'html.parser')
text_content = soup.get_text()

phone_numbers = extract_phone_numbers(text_content)

with open(export, 'w', encoding='utf-8') as file:
    for number in phone_numbers:
        if ' ' not in number and '-' not in number and ' ' not in number and len(number) == 11:
            if number[0] == '8':
                number = '7' + number[1:]
            if number[0] == '7':
                file.write(number + '\n')

print("saved 'phones.txt'.")
