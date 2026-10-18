# Installed GP devkit candidate artifacts

`gp-devkit-candidate.yml` builds one `Honua.Geoprocessing.Cli` candidate from an
exact merged server commit, then qualifies the downloaded package in a separate
job with no server source checkout. The package must carry that source commit in
its repository metadata. The workflow accepts an exact prerelease package
version, a complete .NET 10 SDK version, and an immutable Linux x64 Python base.

The qualification job verifies Microsoft's published SDK SHA-512 and retains
its metadata and archive SHA-256. It installs the CLI from the downloaded local
package feed, runs `workspace bootstrap`, and uses the assembler embedded in that
installed package. The assembler owns the locked default dependencies, sandbox
build, installed service checks, and Python/.NET failure, correction, successful
GeoJSON output, and cancellation fixtures. Release coordination does not maintain
a second sandbox executor or copy scripts from a source checkout.

## Evidence and artifacts

The candidate gate requires all installed transport checks and both language
fixtures to name the same image and packaged assembly. It verifies actual
retained GeoJSON bytes against the successful run receipts. Missing language
results, changed package/output bytes, mismatched images, and output-path escapes
fail the gate.

The workflow retains:

- `gp-devkit-package`: the original package used by qualification.
- `gp-devkit-qualified-candidate`: that package, `devkit-candidate.json`, SDK
  provenance, assembly/installed/run receipts, and verified GeoJSON outputs.
- `gp-devkit-qualified-image`: a saved archive of the exact qualified Docker
  image and its archive checksum. Publication consumes this archive; it must not
  rebuild the image.

Provenance attestations bind the package, candidate document, and saved image to
the producing workflow. The exported proof is revalidated before upload. Service
credentials and local daemon state are not exported.

## Publication boundary

This candidate workflow does not publish a customer release, change the platform
or customer install manifest, or prove server import/map/cloud behavior. A passing
candidate still needs the governed publication path and anonymous consumption
checks before it can satisfy source-free public evaluation.

`gp_devkit_artifacts.py published` verifies that a registry platform manifest has
the exact qualified image configuration digest, then binds the immutable GHCR
reference and public package URL into a release identity. It rejects mutable
references and unqualified image indexes. This command is a byte-identity gate;
it does not upload artifacts or prove that those URLs are anonymously accessible.

The compatible platform manifest and workflow knowledge may advertise the
artifacts only after the separate public-consumption and integrated GIS journey
checks succeed. The current candidate workflow is not evidence that a public
GP package or image has already been released.
