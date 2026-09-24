# Windows Sandbox QA

This runs a packaged Campfire installer in a disposable Windows environment. The
host is only used to stage the installer and collect `result.json` and
`sandbox.log`; Python and Node.js are not supplied to the sandbox.

## Requirements

- Windows Sandbox enabled (`Containers-DisposableClientVM`)
- Windows Pro, Enterprise, Pro Education/SE, or Education. Windows Home is not supported.
- Virtualization enabled in firmware
- Network access for the full model test

On a supported edition, enable it once from an elevated PowerShell window, then
restart Windows:

```powershell
Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -All
```

Do not use scripts that force-install Sandbox component packages on Windows
Home. That configuration is unsupported. On Home, use the free GitHub Actions
Windows runner or a full Windows VM instead.

## Run

From the repository root:

```powershell
pwsh -File qa/windows-sandbox/run-sandbox-qa.ps1
```

Pass an installer explicitly when needed:

```powershell
pwsh -File qa/windows-sandbox/run-sandbox-qa.ps1 -Installer D:\builds\Campfire-Setup.exe
```

`-Quick` checks silent install, bundled-engine startup, health, and uninstall
without downloading models. The default full mode additionally verifies prompt
and file masking. `-Visible` shows the Sandbox window for debugging; otherwise
the host launches it hidden and prints the final JSON result.

Results are copied to `qa/windows-sandbox/test-results/<timestamp>/`. Windows
Sandbox uses the same Windows build as the host, so use a persistent Hyper-V VM
when a different Windows release must be tested.
