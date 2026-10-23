# docs/assets

Committed demo artifacts for docs and the README (not under gitignored `reports/`).

| File | What it is |
|------|------------|
| [`zyvor-dev-mission-control-demo.mp4`](zyvor-dev-mission-control-demo.mp4) | H.264 recording — play on GitHub blob page |
| [`zyvor-dev-mission-control-demo.webm`](zyvor-dev-mission-control-demo.webm) | Original Playwright journey capture |
| [`zyvor-dev-demo.steps`](zyvor-dev-demo.steps) | Step file used to produce that video |
| [`guestkit-mission-control-demo.webm`](guestkit-mission-control-demo.webm) / [`.mp4`](guestkit-mission-control-demo.mp4) | Mission Control → GuestKit flow |
| [`guestkit-github-demo.webm`](guestkit-github-demo.webm) / [`.mp4`](guestkit-github-demo.mp4) | Direct browser journey of the GitHub README |
| [`guestkit-github.steps`](guestkit-github.steps) | Steps used by the MC flow job / direct GitHub recording |
| [`guestkit-product-demo.webm`](guestkit-product-demo.webm) / [`.mp4`](guestkit-product-demo.mp4) | Direct journey of [zyvor.dev/guestkit](https://zyvor.dev/guestkit) |
| [`guestkit-product.steps`](guestkit-product.steps) | Steps for the product-page recording |

Regenerate the Mission Control demo (against a live serve / NodePort):

```bash
node scripts/record-mission-control-demo.mjs http://HOST:30080 docs/assets/guestkit-mission-control-demo.webm
ffmpeg -y -i docs/assets/guestkit-mission-control-demo.webm -c:v libx264 -pix_fmt yuv420p -movflags +faststart -an docs/assets/guestkit-mission-control-demo.mp4
```

Regenerate zyvor.dev demo:

```bash
argus flow run https://zyvor.dev --steps docs/assets/zyvor-dev-demo.steps --video
cp reports/artifacts/flows/cli/journey.webm docs/assets/zyvor-dev-mission-control-demo.webm
ffmpeg -y -i docs/assets/zyvor-dev-mission-control-demo.webm -c:v libx264 -pix_fmt yuv420p -movflags +faststart -an docs/assets/zyvor-dev-mission-control-demo.mp4
```
