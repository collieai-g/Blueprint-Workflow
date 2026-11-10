[CmdletBinding()]
param(
    [string]$OutputDirectory = $(Join-Path $env:LOCALAPPDATA "BlueprintWorkflowPlugin\delivery"),
    [string]$PythonCommand = "python"
)

$ErrorActionPreference = "Stop"
$PluginRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$OutputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
if ($OutputRoot -eq $PluginRoot -or $OutputRoot.StartsWith($PluginRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "The archive must be written outside the plugin source."
}
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$PackageScript = @'
import hashlib, json, pathlib, sys, zipfile
root, output = map(pathlib.Path, sys.argv[1:3])
manifest = json.loads((root / 'plugin.json').read_text(encoding='utf-8'))
assert manifest['$schema'] == 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json'
assert manifest['name'] == 'blueprint-workflow'
assert len(manifest['extensions']['com.openai']['interface']['shortDescription']) <= 30
mcp = json.loads((root / 'mcp.json').read_text(encoding='utf-8'))
assert mcp['$schema'] == 'https://agent-plugins.org/schemas/1.0.0/mcp.schema.json'
assert mcp['mcpServers']['blueprint-workflow']['type'] == 'stdio'
compat = json.loads((root / '.codex-plugin' / 'plugin.json').read_text(encoding='utf-8'))
assert (compat['name'], compat['version']) == (manifest['name'], manifest['version'])
interface = manifest['extensions']['com.openai']['interface']
assert compat['interface'] == interface, 'Compatibility presentation must match the portable manifest'
hooks = json.loads((root / 'hooks' / 'hooks.json').read_text(encoding='utf-8'))
assert set(hooks['hooks']) >= {'SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse'}
allowed = {'plugin.json', 'mcp.json', '.codex-plugin', 'hooks', 'blueprint_workflow', 'skills', 'scripts', 'adapters', 'assets', 'docs', 'pyproject.toml', 'README.md', 'LICENSE', 'NOTICE'}
excluded = {'__pycache__', '.venv', '.pytest_cache', 'tests', '.git'}
files = []
for path in sorted(root.rglob('*')):
    relative = path.relative_to(root)
    if relative.parts[0] not in allowed or any(part in excluded for part in relative.parts): continue
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()): raise ValueError('Links/junctions cannot be packaged: ' + str(relative))
    if path.is_file() and path.suffix not in {'.pyc', '.pyo'}: files.append((path, relative))
required = {'plugin.json', 'mcp.json', '.codex-plugin/plugin.json', 'hooks/hooks.json', 'skills/blueprint-workflow/SKILL.md', 'scripts/launch.py', 'blueprint_workflow/__init__.py'}
actual = {relative.as_posix() for _, relative in files}
assert required <= actual, 'Missing files: ' + repr(required - actual)
for field in ('logo', 'logoDark', 'composerIcon', 'composerIconDark'):
    if reference := interface.get(field):
        relative = str(reference).removeprefix('./')
        asset = pathlib.PurePosixPath(relative)
        assert not asset.is_absolute() and '..' not in asset.parts and relative in actual, 'Missing or unsafe interface asset: ' + field
archive = output / ('blueprint-workflow-' + manifest['version'] + '-local.zip')
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zipped:
    for path, relative in files: zipped.write(path, 'blueprint-workflow/' + relative.as_posix())
with zipfile.ZipFile(archive) as zipped:
    assert zipped.testzip() is None
    names = zipped.namelist()
    assert len(names) == len(set(names))
    for filename in names:
        assert filename.startswith('blueprint-workflow/') and '..' not in pathlib.PurePosixPath(filename).parts
        if filename.endswith('.json'): json.loads(zipped.read(filename))
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
archive.with_suffix(archive.suffix + '.sha256').write_text(digest + '  ' + archive.name + '\n', encoding='ascii')
print(json.dumps({'archive': str(archive), 'sha256': digest, 'files': len(files)}, ensure_ascii=False))
'@
& $PythonCommand -c $PackageScript $PluginRoot $OutputRoot
if ($LASTEXITCODE -ne 0) { throw "Package validation failed." }
