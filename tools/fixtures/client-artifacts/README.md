Recorded public registry responses for WI-4, captured anonymously on 2026-10-02.
`urls.json` maps each response body to its exact HTTPS request URL. Those JSON and
package bodies are unedited; the npm tarball is large because it is the actual
published SDK archive. Tests replay these bytes without live registry requests.

`pypi-metadata.synthetic.json` is not in `urls.json`, is not a PyPI response, and
is not registry evidence. The captured project description is desktop-client
procedure prose, so it is not in this repository (honua-release#427). Recorded-registry
tests serve this synthetic document for the project-metadata URL because the verifier
reads only the artifact filename, sha256, and download URL. The wheel bytes and the
publish attestation stay the captured responses.

`pins.json` contains hand-transcribed primary package pins, not a registry
response. Tests' expected identities were written by hand from registry metadata
and independent `sha256sum` output:

```
679e0873ae1347be0f7de33ae9876ac82ebd4cf1af6a160e8bcf1e8dc70a7b62  npm.tgz
4ca00c6d585a7325ccb39e15c5e3e4e036e3d91b9bed3471cd5037e1e36efcf2  pypi.whl
65e096cdea4d6f2e35226ae3ed3d769fea5f19fc1c4d3612a6339e75a42a8bbd  nuget.nupkg
dcc6bb0477e64982854f38ee704709abafae43e373d7f963af15b23c540859aa  nuget-1.10.0.nupkg
```

npm's `gitHead` is `1102d2d55916340edca13cb28411df8da8206f92`.
NuGet's catalog and nuspec bind 1.10.1 to
`8a0a06c815baefd49e7398d38a9f22642a8c80c5` and 1.10.0 to
`d81067a035854a1bc4c396ed763ba0de6b18864e`. The recorded PyPI publish attestation binds the wheel digest to its signing
certificate. Fulcio source repository digest extension `1.3.6.1.4.1.57264.1.13`
is `12670676a1e8acb835e911c358adbf46a731120a`; the verifier requires this to match
the manifest pin and checks the source repository and envelope signature.
Certificate chain and transparency validation are trusted to PyPI over HTTPS.

NuGet's recorded flat-container index lists 1.6.4 through 1.10.1 and omits 1.6.2.
An anonymous request to
`https://api.nuget.org/v3/registration5-gz-semver2/honua.sdk/1.6.2.json`
also returned HTTP 404. Trunk already pins 1.10.1, so WI-4 does not change the
clientArtifacts publication selection or customer-install pins.

The resolver verifies the manifest's selected publications, then regenerates
component artifact fields and the SDK checkout SHA from those verified identities.
It does not select a publication from a moving registry channel. A changed
clientArtifacts pin must verify before the SDK source and artifact can move together.
