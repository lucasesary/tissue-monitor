import smtplib, os
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")
user = os.environ["IMAP_USER"]
pwd  = os.environ["IMAP_PASS"]

arq = Path(__file__).parent / "dados" / "qualidade" / "Dados Qualidade_Maio26.xlsx"

msg = MIMEMultipart()
msg["From"]    = user
msg["To"]      = user
msg["Subject"] = "Dados Qualidade_Maio26 - AT1"
msg.attach(MIMEText("Planilha de qualidade maio/26.", "plain"))

part = MIMEBase("application", "octet-stream")
part.set_payload(arq.read_bytes())
encoders.encode_base64(part)
part.add_header("Content-Disposition", f'attachment; filename="{arq.name}"')
msg.attach(part)

with smtplib.SMTP("smtp.gmail.com", 587) as s:
    s.ehlo()
    s.starttls()
    s.login(user, pwd)
    s.sendmail(user, user, msg.as_string())

print("E-mail enviado com sucesso.")
