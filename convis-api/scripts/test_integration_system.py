#!/usr/bin/env python3
"""
Quick Integration System Test Script
Run this to verify the integration system works correctly
"""
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from bson import ObjectId

def test_encryption():
    """Test credential encryption/decryption"""
    print("\n" + "="*60)
    print("TEST 1: Credential Encryption/Decryption")
    print("="*60)

    from app.services.integrations.credentials_encryption import credentials_encryption

    test_user_id = "test_user_123"

    # Test Jira credentials
    jira_creds = {
        "base_url": "https://company.atlassian.net",
        "email": "dev@company.com",
        "api_token": "ATATT3xFfGF0_super_secret_token_123",
        "default_project": "SUPPORT"
    }

    print(f"\nOriginal credentials:")
    print(f"  - base_url: {jira_creds['base_url']}")
    print(f"  - email: {jira_creds['email']}")
    print(f"  - api_token: {jira_creds['api_token'][:10]}...")

    # Encrypt
    encrypted = credentials_encryption.encrypt_credentials(jira_creds, test_user_id)

    print(f"\nEncrypted credentials:")
    print(f"  - base_url: {encrypted['base_url']} (unchanged)")
    print(f"  - email: {encrypted['email']} (unchanged)")
    print(f"  - api_token: {encrypted['api_token']['_encrypted']} (encrypted={encrypted['api_token']['_encrypted']})")

    # Decrypt
    decrypted = credentials_encryption.decrypt_credentials(encrypted, test_user_id)

    print(f"\nDecrypted credentials:")
    print(f"  - api_token matches: {decrypted['api_token'] == jira_creds['api_token']}")

    assert decrypted == jira_creds, "Decrypted credentials don't match original!"
    print("\n✅ Encryption/Decryption test PASSED")
    return True


def test_models():
    """Test Pydantic models"""
    print("\n" + "="*60)
    print("TEST 2: Integration Models")
    print("="*60)

    from app.models.integration import (
        JiraCredentials, HubSpotCredentials, EmailCredentials,
        Integration, IntegrationType, IntegrationStatus
    )

    # Test JiraCredentials
    jira = JiraCredentials(
        base_url="https://test.atlassian.net",
        email="test@test.com",
        api_token="test-token"
    )
    print(f"\n✅ JiraCredentials created: base_url={jira.base_url}")

    # Test HubSpotCredentials
    hubspot = HubSpotCredentials(access_token="pat-na1-xxx")
    print(f"✅ HubSpotCredentials created: access_token={hubspot.access_token[:10]}...")

    # Test EmailCredentials
    email = EmailCredentials(
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_username="user@gmail.com",
        smtp_password="password",
        from_email="user@gmail.com"
    )
    print(f"✅ EmailCredentials created: smtp_host={email.smtp_host}")

    # Test Integration model
    integration = Integration(
        user_id="user123",
        name="My Jira",
        type=IntegrationType.JIRA,
        credentials={"masked": True},
        status=IntegrationStatus.ACTIVE
    )
    print(f"✅ Integration model created: name={integration.name}, type={integration.type}")

    print("\n✅ Models test PASSED")
    return True


def test_template_renderer():
    """Test template rendering"""
    print("\n" + "="*60)
    print("TEST 3: Template Renderer")
    print("="*60)

    from app.services.integrations.template_renderer import TemplateRenderer

    call_data = {
        "caller_phone": "+1234567890",
        "caller_name": "John Doe",
        "call_summary": "User reported login issues",
        "issue_category": "Technical Support",
        "customer_email": "john@example.com",
        "call": {
            "duration": 120,
            "agent": "Bot-1"
        }
    }

    # Test basic template
    template1 = "Support ticket from {{caller_name}}: {{issue_category}}"
    result1 = TemplateRenderer.render(template1, call_data)
    print(f"\nTemplate: {template1}")
    print(f"Result: {result1}")
    assert "John Doe" in result1

    # Test nested data
    template2 = "Call handled by {{call.agent}} lasting {{call.duration}} seconds"
    result2 = TemplateRenderer.render(template2, call_data)
    print(f"\nTemplate: {template2}")
    print(f"Result: {result2}")
    assert "Bot-1" in result2

    # Test filter
    template3 = "{{caller_name|upper}}"
    result3 = TemplateRenderer.render(template3, call_data)
    print(f"\nTemplate: {template3}")
    print(f"Result: {result3}")
    assert result3 == "JOHN DOE"

    print("\n✅ Template Renderer test PASSED")
    return True


def test_jira_service_init():
    """Test Jira service initialization"""
    print("\n" + "="*60)
    print("TEST 4: Jira Service Initialization")
    print("="*60)

    from app.models.integration import JiraCredentials
    from app.services.integrations.jira_service import JiraService

    creds = JiraCredentials(
        base_url="https://company.atlassian.net",
        email="dev@company.com",
        api_token="test-token-123",
        default_project="PROJ"
    )

    service = JiraService(creds)

    print(f"\n✅ JiraService created:")
    print(f"  - base_url: {service.base_url}")
    print(f"  - email: {service.email}")
    print(f"  - default_project: {service.default_project}")
    print(f"  - auth configured: {service.auth is not None}")

    print("\n✅ Jira Service init test PASSED")
    return True


def test_full_flow():
    """Test full integration flow"""
    print("\n" + "="*60)
    print("TEST 5: Full Integration Flow (End-to-End)")
    print("="*60)

    from app.services.integrations.credentials_encryption import credentials_encryption
    from app.models.integration import JiraCredentials, Integration, IntegrationType
    from app.services.integrations.jira_service import JiraService
    from app.services.integrations.template_renderer import TemplateRenderer

    user_id = "e2e_test_user_456"

    # Step 1: User provides credentials
    print("\nStep 1: User provides credentials")
    raw_creds = {
        "base_url": "https://mycompany.atlassian.net",
        "email": "dev@mycompany.com",
        "api_token": "ATATT3xFfGF0_production_token_xyz",
        "default_project": "CALLS"
    }

    # Step 2: Validate with Pydantic
    print("Step 2: Validate with Pydantic model")
    validated = JiraCredentials(**raw_creds)
    print(f"  ✅ Credentials validated")

    # Step 3: Encrypt for storage
    print("Step 3: Encrypt for storage")
    encrypted = credentials_encryption.encrypt_credentials(validated.dict(), user_id)
    print(f"  ✅ Credentials encrypted (api_token._encrypted={encrypted['api_token']['_encrypted']})")

    # Step 4: Simulate MongoDB storage
    print("Step 4: Simulate MongoDB storage")
    stored_doc = {
        "_id": str(ObjectId()),
        "user_id": user_id,
        "name": "Production Jira",
        "type": "jira",
        "credentials": encrypted,
        "status": "active",
        "is_active": True,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }
    print(f"  ✅ Integration stored with ID: {stored_doc['_id']}")

    # Step 5: Retrieve and decrypt (workflow execution)
    print("Step 5: Retrieve and decrypt for workflow")
    decrypted = credentials_encryption.decrypt_credentials(stored_doc["credentials"], user_id)
    print(f"  ✅ Credentials decrypted")

    # Step 6: Create service
    print("Step 6: Create Jira service instance")
    service_creds = JiraCredentials(**decrypted)
    service = JiraService(service_creds)
    print(f"  ✅ JiraService created for {service.base_url}")

    # Step 7: Test template rendering with call data
    print("Step 7: Render workflow templates")
    call_context = {
        "caller_name": "Alice Smith",
        "issue_description": "Website performance issues",
        "customer_email": "alice@customer.com"
    }

    issue_config = {
        "summary": "Support: {{caller_name}} - Issue Report",
        "description": "Customer: {{caller_name}}\nEmail: {{customer_email}}\n\nIssue: {{issue_description}}"
    }

    rendered_summary = TemplateRenderer.render(issue_config["summary"], call_context)
    rendered_desc = TemplateRenderer.render(issue_config["description"], call_context)

    print(f"  ✅ Summary: {rendered_summary}")
    print(f"  ✅ Description rendered (contains customer info)")

    assert "Alice Smith" in rendered_summary
    assert "alice@customer.com" in rendered_desc

    print("\n✅ Full Flow test PASSED")
    return True


def run_all_tests():
    """Run all tests"""
    print("\n" + "#"*60)
    print("#" + " "*20 + "INTEGRATION SYSTEM TESTS" + " "*14 + "#")
    print("#"*60)

    tests = [
        ("Encryption/Decryption", test_encryption),
        ("Models", test_models),
        ("Template Renderer", test_template_renderer),
        ("Jira Service Init", test_jira_service_init),
        ("Full End-to-End Flow", test_full_flow),
    ]

    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result, None))
        except Exception as e:
            print(f"\n❌ {name} test FAILED: {e}")
            results.append((name, False, str(e)))

    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)

    passed = sum(1 for _, result, _ in results if result)
    total = len(results)

    for name, result, error in results:
        status = "✅ PASS" if result else f"❌ FAIL: {error}"
        print(f"  {name}: {status}")

    print(f"\nTotal: {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 ALL TESTS PASSED! Integration system is working correctly.")
        return 0
    else:
        print("\n⚠️  Some tests failed. Please review the errors above.")
        return 1


if __name__ == "__main__":
    exit_code = run_all_tests()
    sys.exit(exit_code)
