import smtplib
import time
from email.message import EmailMessage

def email_alert(subject, body, to):
    msg = EmailMessage()
    msg.set_content(body)
    msg['subject'] = subject
    msg['to'] = to

    user = "mirshantm@gmail.com"  # Replace with your email
    msg['from'] = user
    password = "rwos jfao vwku sriu"  # Replace with your email password or app password

    server = smtplib.SMTP("smtp.gmail.com", 587)
    server.starttls()
    server.login(user, password)
    server.send_message(msg)

    server.quit()
# Send the alert 15 times with a 2-second break
for i in range(15):
    email_alert("ALERT", "ALERT", "jefinjohn07@gmail.com")  # Replace with recipient's email
    print(f"Email {i + 1} sent!")
    time.sleep(2) 