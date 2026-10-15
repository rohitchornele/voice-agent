from livekit import api

SECRET = "<the secret from your livekit.yaml>"

def make(identity):
    return (
        api.AccessToken("devkey", SECRET)
        .with_identity(identity)
        .with_grants(api.VideoGrants(room_join=True, room="test"))
        .to_jwt()
    )

print("alice:", make("alice"))
print("bob:", make("bob"))