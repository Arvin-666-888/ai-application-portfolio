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


def test_jwt_shop_claim_must_match_database_user(client):
    import jwt

    from app.config import settings
    from app.services.auth_service import create_access_token

    valid = create_access_token(1, "shop-us")
    payload = jwt.decode(valid, settings.SECRET_KEY, algorithms=["HS256"])
    payload["shop_id"] = "shop-eu"
    forged = jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")

    response = client.get(
        "/api/v1/orders", headers={"Authorization": f"Bearer {forged}"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid access token"


def test_jwt_requires_shop_claim(client):
    import jwt

    from app.config import settings
    from app.services.auth_service import create_access_token

    payload = jwt.decode(
        create_access_token(1, "shop-us"), settings.SECRET_KEY, algorithms=["HS256"]
    )
    payload.pop("shop_id")
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")
    response = client.get(
        "/api/v1/orders", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
