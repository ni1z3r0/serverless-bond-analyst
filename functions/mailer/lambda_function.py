import json
import boto3
import os  

ses_client = boto3.client('ses')

# Load environment variables safely
# If these are missing, we default to None, which might error later if not set in Console
SENDER_EMAIL = os.environ.get('SENDER_EMAIL') 
TARGET_EMAIL = os.environ.get('TARGET_EMAIL') 

def lambda_handler(event, context):
    print("Received event:", json.dumps(event)) # Debug log to see incoming data
    
    # SQS events arrive with a list of Records
    for record in event['Records']:
        try:
            # Extract the payload created by the first Lambda
            message_body = record['body']
            print("Processing message body:", message_body)
            
            message_data = json.loads(message_body)
            
            subject = message_data.get('subject', 'Bond Alert')
            report = message_data.get('report_summary', 'No content provided.')
            recipient = message_data.get('recipient', TARGET_EMAIL) # Use payload or default

            if not recipient or not SENDER_EMAIL:
                print("CRITICAL: Missing Sender or Recipient email configuration.")
                return "Error: Configuration Missing"

            # --- SES SEND EMAIL LOGIC ---
            print(f"Attempting to send email from {SENDER_EMAIL} to {recipient}...")
            ses_client.send_email(
                Source=SENDER_EMAIL,
                Destination={'ToAddresses': [recipient]},
                Message={
                    'Subject': {'Data': subject},
                    'Body': {'Text': {'Data': report}}
                }
            )
            print(f"✅ Email sent to {recipient} successfully.")

        except Exception as e:
            # If SES fails, raising the exception tells SQS to retry the message
            print(f"❌ FATAL ERROR sending email: {e}")
            raise e 

    return 'Successfully processed SQS records.'