import os, sys
from dotenv import load_dotenv
from livekit import api

load_dotenv()
room, identity = sys.argv[1], sys.argv[2]
token = (
    api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
    .with_identity(identity)
    .with_grants(api.VideoGrants(room_join=True, room=room))
    .to_jwt()
)

print(token)