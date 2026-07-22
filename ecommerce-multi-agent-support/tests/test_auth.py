def test_register_and_login(client):
    register = client.post(
        "/api/v1/auth/register",
        json={"username": "new_user", "password": "strong-pass-123"},
    )
    assert register.status_code == 201
    assert register.json()["username"] == "new_user"

    login = client.post(
        "/api/v1/auth/login",
        json={"username": "new_user", "password": "strong-pass-123"},
    )
    assert login.status_code == 200
    assert login.json()["token_type"] == "bearer"


def test_protected_endpoint_requires_token(client):
    response = client.get("/api/v1/orders")
    assert response.status_code == 401
