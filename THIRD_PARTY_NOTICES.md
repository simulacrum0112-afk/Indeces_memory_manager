# Third-party sources

`indeces/memory.py` adapts the NPMI, source-context gating, and Hebbian update strategy from [Mingyuliu-botaaa/hebbian-mark-graph](https://github.com/Mingyuliu-botaaa/hebbian-mark-graph), pinned to commit `8769b9ed0af3531b9fbc09fb6e084d8e7adf724d`. The original complete code is not bundled. Integration differences are documented in `docs/BASELINE.md`.

Upstream license:

```text
MIT License

Copyright (c) 2026 Mingyuliu-botaaa

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Yuki was inspected as an architectural reference for stateless transport, independent stage allowances, and whole-interaction watermark maintenance. No Yuki package, private configuration, runtime data, or source file is bundled or imported.

## Implementation checkpoint

Documentation was reconciled on 2026-10-01 against Indeces 0.10.0. The public code commit, CI and evidence boundaries are listed in [docs/CHECKPOINT.md](docs/CHECKPOINT.md). The current reply path uses static NPMI; dynamic updates remain shadow history, as detailed in [docs/BASELINE.md](docs/BASELINE.md). This documentation update does not change the pinned upstream revision or the license text above.

The 0.10.0 Yuki reference was limited to three tracked design documents for lifecycle ownership and call contracts. External design articles cited in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) are references only; no new framework or telemetry dependency was added.
