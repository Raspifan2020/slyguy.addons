CHANNELS = """
query {
  linearChannelGroups {
    id
    title
    channels {
      __typename
      id
      title
      number
      tileImage {
        uri
      }
    }
  }
}
"""

PLAYBACK = """
mutation startLinearPlayback ($channelId: ID!, $deviceId: ID!) {
    startLinearPlayback(channelId: $channelId, deviceId: $deviceId) {
      __typename
      ... on LinearPlaybackSources {
... on PlaybackSources {
  playbackSource(drmType: WIDEVINE) {
    streamUri
    drmLicense {
      __typename
      licenseUri
        ... on FairplayLicense {
          certificateUri
      }
    }
    emeHeaders {
      name
      value
    }
  }
}
      }
  ... on SubscriptionNeeded {
    subscriptions {
      id
      title
    }
  }
    }
  }
"""