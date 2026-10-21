from x_algo_watch import deployment


def test_deployment_audit_does_not_treat_inaccessible_repo_as_ready(monkeypatch):
    monkeypatch.setattr(deployment, "query", lambda *args: {"unavailable": True})
    assert deployment.audit("owner/repo") == {"repository": "owner/repo", "repository_accessible": False, "ready": False}


def test_unrestricted_environment_and_repository_secret_fail_audit(monkeypatch):
    values = {
        "": {"visibility": "public", "default_branch": "main", "security_and_analysis": {"secret_scanning": {"status": "enabled"}, "secret_scanning_push_protection": {"status": "enabled"}}},
        "/actions/permissions/workflow": {"default_workflow_permissions": "read"},
        "/environments/openrouter": {"deployment_branch_policy": None},
        "/environments/openrouter/deployment-branch-policies": {"branch_policies": []},
        "/environments/openrouter/secrets": {"secrets": [{"name": "OPENROUTER_API_KEY"}]},
        "/actions/secrets": {"secrets": [{"name": "OPENROUTER_API_KEY"}]},
        "/actions/variables": {"variables": [{"name": "DRAFT_ENABLED", "value": "true"}]},
    }
    monkeypatch.setattr(deployment, "query", lambda repository, suffix: values[suffix])
    result = deployment.audit("owner/repo")
    assert not result["ready"] and not result["checks"]["main_only"]
    assert not result["checks"]["no_repository_model_key"]
