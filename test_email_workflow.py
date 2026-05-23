"""
Test script for email workflow integration
Run this to verify email workflows are working correctly
"""
import requests
import json
from datetime import datetime

# Configuration
API_URL = "http://localhost:8000"
TOKEN = "YOUR_JWT_TOKEN_HERE"  # Get this from localStorage after login

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

def create_email_integration():
    """Step 1: Create an email integration"""
    print("\n1. Creating email integration...")

    data = {
        "name": "Test Gmail SMTP",
        "type": "email",
        "credentials": {
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "smtp_username": "your-email@gmail.com",
            "smtp_password": "your-app-password",  # Gmail App Password
            "from_email": "your-email@gmail.com",
            "from_name": "Convis AI Test",
            "use_tls": True
        }
    }

    response = requests.post(
        f"{API_URL}/api/integrations/",
        headers=headers,
        json=data
    )

    if response.status_code == 201:
        result = response.json()
        integration_id = result.get("integration_id")
        print(f"✅ Email integration created: {integration_id}")
        return integration_id
    else:
        print(f"❌ Failed to create integration: {response.text}")
        return None


def test_email_connection(integration_id):
    """Step 2: Test email connection"""
    print(f"\n2. Testing email connection...")

    response = requests.post(
        f"{API_URL}/api/integrations/{integration_id}/test",
        headers=headers
    )

    if response.status_code == 200:
        result = response.json()
        if result.get("success"):
            print(f"✅ Email connection successful")
            return True
        else:
            print(f"❌ Connection failed: {result.get('message')}")
            return False
    else:
        print(f"❌ Test failed: {response.text}")
        return False


def create_email_workflow(integration_id):
    """Step 3: Create a workflow to send emails after calls"""
    print(f"\n3. Creating email workflow...")

    data = {
        "name": "Auto Send Call Summary Email",
        "description": "Automatically send call summary to customers who provide their email",
        "trigger_event": "call_completed",
        "conditions": [
            {
                "field": "email_mentioned",
                "operator": "equals",
                "value": True,
                "logic": "AND"
            },
            {
                "field": "customer_email",
                "operator": "exists",
                "value": True
            }
        ],
        "actions": [
            {
                "type": "send_email",
                "integration_id": integration_id,
                "config": {
                    "to": "{{customer_email}}",
                    "subject": "Your Call Summary - {{customer_name}}",
                    "format": "html",
                    "body": """
                    <html>
                    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                            <h2 style="color: #2563eb;">Thank You for Your Call</h2>

                            <p>Hi {{customer_name}},</p>

                            <p>Thank you for speaking with us. Here's a summary of our conversation:</p>

                            <div style="background-color: #f3f4f6; padding: 15px; border-radius: 5px; margin: 20px 0;">
                                <h3 style="margin-top: 0; color: #1f2937;">Call Summary</h3>
                                <p>{{call.summary}}</p>
                            </div>

                            <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                                <tr>
                                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;"><strong>Duration:</strong></td>
                                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">{{call.duration}} seconds</td>
                                </tr>
                                <tr>
                                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;"><strong>Date:</strong></td>
                                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">{{call.created_at}}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;"><strong>Sentiment:</strong></td>
                                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">{{call.sentiment}}</td>
                                </tr>
                            </table>

                            {{#if appointment_booked}}
                            <div style="background-color: #dcfce7; padding: 15px; border-radius: 5px; margin: 20px 0; border-left: 4px solid #16a34a;">
                                <h3 style="margin-top: 0; color: #15803d;">Appointment Confirmed</h3>
                                <p>Your appointment has been scheduled for: <strong>{{appointment_date}}</strong></p>
                            </div>
                            {{/if}}

                            <p>If you have any questions, feel free to reach out to us.</p>

                            <p style="margin-top: 30px;">
                                Best regards,<br>
                                <strong>The Convis AI Team</strong>
                            </p>

                            <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 20px 0;">

                            <p style="font-size: 12px; color: #6b7280;">
                                This email was automatically generated by Convis AI based on your recent call.
                            </p>
                        </div>
                    </body>
                    </html>
                    """
                },
                "on_error": "continue"
            }
        ],
        "is_active": True,
        "priority": 1
    }

    response = requests.post(
        f"{API_URL}/api/workflows/",
        headers=headers,
        json=data
    )

    if response.status_code == 201:
        result = response.json()
        workflow_id = result.get("workflow_id")
        print(f"✅ Workflow created: {workflow_id}")
        return workflow_id
    else:
        print(f"❌ Failed to create workflow: {response.text}")
        return None


def list_workflows():
    """Step 4: List all workflows"""
    print(f"\n4. Listing workflows...")

    response = requests.get(
        f"{API_URL}/api/workflows/",
        headers=headers
    )

    if response.status_code == 200:
        result = response.json()
        workflows = result.get("workflows", [])
        print(f"✅ Found {len(workflows)} workflows:")
        for wf in workflows:
            print(f"   - {wf.get('name')} (ID: {wf.get('_id')}, Active: {wf.get('is_active')})")
        return workflows
    else:
        print(f"❌ Failed to list workflows: {response.text}")
        return []


def main():
    """Run the complete test"""
    print("=" * 60)
    print("Email Workflow Integration Test")
    print("=" * 60)

    if TOKEN == "YOUR_JWT_TOKEN_HERE":
        print("\n⚠️  Please set your JWT token in the script first!")
        print("   1. Login to the application")
        print("   2. Open browser console")
        print("   3. Run: localStorage.getItem('token')")
        print("   4. Copy the token and update TOKEN variable in this script")
        return

    # Step 1: Create email integration
    integration_id = create_email_integration()
    if not integration_id:
        print("\n❌ Setup failed at step 1")
        return

    # Step 2: Test connection
    if not test_email_connection(integration_id):
        print("\n❌ Setup failed at step 2")
        print("   Check your email credentials (especially app password for Gmail)")
        return

    # Step 3: Create workflow
    workflow_id = create_email_workflow(integration_id)
    if not workflow_id:
        print("\n❌ Setup failed at step 3")
        return

    # Step 4: List workflows
    list_workflows()

    print("\n" + "=" * 60)
    print("✅ Setup Complete!")
    print("=" * 60)
    print("\nNext steps:")
    print("1. Make a test call through your system")
    print("2. During the call, say: 'Please send the summary to test@example.com'")
    print("3. After the call completes, check the test@example.com inbox")
    print("4. You should receive an email with the call summary!")
    print("\nTo view workflow executions:")
    print(f"   GET {API_URL}/api/workflows/executions")


if __name__ == "__main__":
    main()
