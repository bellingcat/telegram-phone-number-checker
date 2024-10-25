with open(r'C:\Users\danil\PycharmProjects\pythonProject2\phones.txt', 'r', encoding='utf-8') as file:
    phones = file.readlines()

unique_phones = set(phone.strip() for phone in phones)

with open(r'C:\Users\danil\PycharmProjects\pythonProject2\phones.txt', 'w', encoding='utf-8') as file:
    for phone in sorted(unique_phones):
        file.write(f"{phone}\n")

print("deleted.")