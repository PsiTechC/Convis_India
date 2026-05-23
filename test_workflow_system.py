#!/usr/bin/env python3
"""
Workflow System Test Script

This script tests the Convis workflow system by:
1. Creating a test workflow
2. Manually triggering it with sample call data
3. Checking the execution results

Usage:
    python test_workflow_system.py --token YOUR_AUTH_TOKEN

Requirements:
    pip install requests
"""

import requests
import json
import argparse
import time
from typing import Dict, Any, Optional

API_URL = "http://localhost:8000"  # Change to your API URL


class WorkflowTester:
    def __init__(self, api_url: str, auth_token: str):
        self.api_url = api_url.rstrip('/')
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"
        }

    def create_test_workflow(self) -> Optional[str]:
        """Create a test workflow for email automation"""
        print("\n📝 Creating test workflow...")

        workflow = {
            "name": "Test: Send Email After Positive Calls",
            "description": "Test workflow to send email after positive customer calls",
            "trigger_event": "call_completed",
            "is_active": True,
            "priority": 1,
            "conditions": [
                {
                    "field": "sentiment",
                    "operator": "equals",
                    "value": "positive"
                },
                {
                    "field": "email_mentioned",
                    "operator": "equals",
                    "value": True
                }
            ],
            "actions": [
                {
                    "type": "send_email",
                    "integration_id": "test-integration-id",  # Replace with real integration ID
                    "config": {
                        "to": "{{customer_email}}",
                        "subject": "Thanks for calling, {{customer_name}}!",
                        "body_html": "<h1>Hi {{customer_name}},</h1><p>Thank you for your call! Here's a summary:</p><p><em>{{summary}}</em></p>",
                        "body_text": "Hi {{customer_name}},\n\nThank you for your call! Summary: {{summary}}"
                    },
                    "timeout_seconds": 30,
                    "retry_on_failure": True,
                    "max_retries": 3,
                    "continue_on_error": True
                }
            ]
        }

        try:
            response = requests.post(
                f"{self.api_url}/api/workflows/",
                headers=self.headers,
                json=workflow
            )
            response.raise_for_status()

            data = response.json()
            workflow_id = data.get("_id") or data.get("id")
            print(f"✅ Workflow created successfully! ID: {workflow_id}")
            return workflow_id

        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to create workflow: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"   Response: {e.response.text}")
            return None

    def list_workflows(self) -> list:
        """List all workflows"""
        print("\n📋 Listing workflows...")

        try:
            response = requests.get(
                f"{self.api_url}/api/workflows/",
                headers=self.headers
            )
            response.raise_for_status()

            workflows = response.json()
            print(f"✅ Found {len(workflows)} workflows:")
            for wf in workflows:
                status = "🟢 Active" if wf.get("is_active") else "🔴 Inactive"
                print(f"   {status} [{wf.get('_id') or wf.get('id')}] {wf.get('name')}")

            return workflows

        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to list workflows: {e}")
            return []

    def get_workflow_details(self, workflow_id: str) -> Optional[Dict]:
        """Get workflow details"""
        print(f"\n🔍 Fetching workflow details for {workflow_id}...")

        try:
            response = requests.get(
                f"{self.api_url}/api/workflows/{workflow_id}",
                headers=self.headers
            )
            response.raise_for_status()

            workflow = response.json()
            print(f"✅ Workflow: {workflow.get('name')}")
            print(f"   Trigger: {workflow.get('trigger_event')}")
            print(f"   Conditions: {len(workflow.get('conditions', []))}")
            print(f"   Actions: {len(workflow.get('actions', []))}")
            print(f"   Executions: {workflow.get('execution_count', 0)}")
            print(f"   Success Rate: {workflow.get('success_count', 0)}/{workflow.get('execution_count', 0)}")

            return workflow

        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to get workflow: {e}")
            return None

    def execute_workflow(self, workflow_id: str, trigger_data: Dict[str, Any]) -> Optional[str]:
        """Manually execute a workflow with test data"""
        print(f"\n🚀 Executing workflow {workflow_id}...")

        try:
            response = requests.post(
                f"{self.api_url}/api/workflows/{workflow_id}/execute",
                headers=self.headers,
                json={"trigger_data": trigger_data}
            )
            response.raise_for_status()

            result = response.json()
            execution_id = result.get("execution_id")
            print(f"✅ Workflow executed! Execution ID: {execution_id}")
            return execution_id

        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to execute workflow: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"   Response: {e.response.text}")
            return None

    def get_execution_results(self, workflow_id: str, execution_id: str = None) -> list:
        """Get workflow execution results"""
        print(f"\n📊 Fetching execution results for workflow {workflow_id}...")

        try:
            response = requests.get(
                f"{self.api_url}/api/workflows/{workflow_id}/executions",
                headers=self.headers
            )
            response.raise_for_status()

            data = response.json()
            executions = data.get("executions", [])

            if not executions:
                print("⚠️  No executions found")
                return []

            print(f"✅ Found {len(executions)} executions:")
            for ex in executions[:5]:  # Show last 5
                status_icon = {
                    "completed": "✅",
                    "failed": "❌",
                    "partial": "⚠️",
                    "running": "🔄"
                }.get(ex.get("status"), "❓")

                print(f"   {status_icon} [{ex.get('_id')}] {ex.get('status')}")
                print(f"      Actions: {ex.get('actions_executed', 0)}")
                print(f"      Duration: {ex.get('duration_ms', 0)}ms")
                if ex.get('error_message'):
                    print(f"      Error: {ex.get('error_message')}")

            return executions

        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to get executions: {e}")
            return []

    def test_workflow_system(self):
        """Run complete workflow system test"""
        print("=" * 60)
        print("🧪 WORKFLOW SYSTEM TEST")
        print("=" * 60)

        # Step 1: List existing workflows
        workflows = self.list_workflows()

        # Step 2: Create test workflow (or use existing)
        if workflows:
            print("\n❓ Use existing workflow? (y/n): ", end="")
            use_existing = input().strip().lower() == 'y'

            if use_existing:
                workflow_id = workflows[0].get('_id') or workflows[0].get('id')
                print(f"✅ Using existing workflow: {workflow_id}")
            else:
                workflow_id = self.create_test_workflow()
        else:
            workflow_id = self.create_test_workflow()

        if not workflow_id:
            print("\n❌ Cannot continue without a workflow")
            return

        # Step 3: Get workflow details
        self.get_workflow_details(workflow_id)

        # Step 4: Create test trigger data
        print("\n📦 Creating test trigger data...")
        trigger_data = {
            "call_id": "test-call-123",
            "call": {
                "id": "test-call-123",
                "status": "completed",
                "duration": 120,
                "direction": "inbound",
                "from_number": "+1234567890",
                "to_number": "+0987654321",
                "transcription": "Hello, I was calling to inquire about your pricing for the enterprise plan. My email is john.doe@example.com.",
                "summary": "Customer inquired about enterprise pricing and provided email.",
                "sentiment": "positive",
                "sentiment_score": 0.85,
                "created_at": "2024-01-15T10:30:00Z",
                "ended_at": "2024-01-15T10:32:00Z",
                "recording_url": "https://example.com/recording.mp3"
            },
            "customer": {
                "name": "John Doe",
                "phone": "+1234567890",
                "email": "john.doe@example.com"
            },
            "customer_name": "John Doe",
            "customer_email": "john.doe@example.com",
            "customer_phone": "+1234567890",
            "email_mentioned": True,
            "appointment_booked": False,
            "appointment_date": None,
            "sentiment": "positive",
            "agent": {
                "name": "AI Assistant",
                "email": "ai@company.com"
            },
            "campaign_id": None,
            "metadata": {},
            "timestamp": "2024-01-15T10:32:05Z"
        }

        print("✅ Test data created")
        print(f"   Customer: {trigger_data['customer_name']}")
        print(f"   Email: {trigger_data['customer_email']}")
        print(f"   Sentiment: {trigger_data['sentiment']}")

        # Step 5: Execute workflow
        execution_id = self.execute_workflow(workflow_id, trigger_data)

        if not execution_id:
            print("\n❌ Workflow execution failed")
            return

        # Step 6: Wait a moment for execution to complete
        print("\n⏳ Waiting for execution to complete...")
        time.sleep(3)

        # Step 7: Get execution results
        executions = self.get_execution_results(workflow_id, execution_id)

        # Summary
        print("\n" + "=" * 60)
        print("📈 TEST SUMMARY")
        print("=" * 60)

        if executions:
            latest = executions[0]
            print(f"✅ Workflow executed successfully")
            print(f"   Status: {latest.get('status')}")
            print(f"   Actions executed: {latest.get('actions_executed', 0)}")
            print(f"   Duration: {latest.get('duration_ms', 0)}ms")
            print(f"   Conditions met: {latest.get('conditions_met', False)}")

            if latest.get('error_message'):
                print(f"⚠️  Error: {latest.get('error_message')}")
        else:
            print("⚠️  No execution results found")

        print("\n✅ Test completed!")


def main():
    parser = argparse.ArgumentParser(description="Test the Convis workflow system")
    parser.add_argument(
        "--token",
        required=True,
        help="Authentication token"
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="API URL (default: http://localhost:8000)"
    )

    args = parser.parse_args()

    tester = WorkflowTester(args.api_url, args.token)
    tester.test_workflow_system()


if __name__ == "__main__":
    main()
