// Explicit operator action only. No automatic retention, overwrite or live restore.
const fs = require('fs');
const crypto = require('crypto');
const {spawnSync} = require('child_process');
const action = process.env.CATALOG_BACKUP_ACTION || 'backup';
const token = crypto.randomUUID().replaceAll('-', '');
const temporary = `/tmp/catalog-tool-${token}.json`;
function digest(path) { return crypto.createHash('sha256').update(fs.readFileSync(path)).digest('hex'); }
function tool(binary, args, identity) {
  fs.writeFileSync(temporary, JSON.stringify({
    uri: `mongodb://${identity.username}@127.0.0.1:27017/?authSource=${identity.auth_source}`,
    password: identity.password
  }), {flag: 'wx', mode: 0o600});
  try {
    const result = spawnSync(binary, ['--config', temporary, '--quiet', ...args], {encoding: 'utf8', timeout: 60000});
    if (result.status !== 0) {
      const diagnostic = (result.stderr || String(result.error || 'Database tool failed'))
        .replaceAll(identity.password, '[REDACTED]');
      fs.writeFileSync('/tmp/catalog-tool-error.log', diagnostic, {mode: 0o600});
      throw new Error('Database tool failed');
    }
  } finally { fs.unlinkSync(temporary); }
}
try {
  if (action === 'backup') {
    if (process.env.CATALOG_WRITERS_QUIESCED !== 'true') throw new Error('Quiesce writers explicitly');
    const secret = JSON.parse(fs.readFileSync('/run/secrets/reader.json', 'utf8'));
    const pending = `/backups/.pending-${token}`;
    fs.mkdirSync(pending, {mode: 0o700});
    const archive = `${pending}/catalog.archive.gz`;
    tool('mongodump', ['--db', 'retailflow', `--archive=${archive}`, '--gzip'], secret);
    const handle = fs.openSync(archive, 'r'); fs.fsyncSync(handle); fs.closeSync(handle);
    const manifest = {format_version: 1, source_database: 'retailflow', created_at: new Date().toISOString(),
      bytes: fs.statSync(archive).size, sha256: digest(archive)};
    const metadata = fs.openSync(`${pending}/manifest.json`, 'wx', 0o600);
    fs.writeFileSync(metadata, JSON.stringify(manifest)); fs.fsyncSync(metadata); fs.closeSync(metadata);
    fs.renameSync(pending, `/backups/${token}`);
    print(JSON.stringify({status: 'complete', bundle: token}));
  } else if (action === 'restore') {
    const bundle = process.env.CATALOG_BACKUP_BUNDLE || '';
    if (!/^[a-f0-9]{32}$/.test(bundle)) throw new Error('Invalid backup bundle');
    const folder = `/backups/${bundle}`;
    const metadata = JSON.parse(fs.readFileSync(`${folder}/manifest.json`, 'utf8'));
    const archive = `${folder}/catalog.archive.gz`;
    if (metadata.format_version !== 1 || metadata.source_database !== 'retailflow' ||
        metadata.bytes !== fs.statSync(archive).size || metadata.sha256 !== digest(archive)) throw new Error('Backup integrity failed');
    const identity = {username: fs.readFileSync('/run/secrets/admin-user', 'utf8'),
      password: fs.readFileSync('/run/secrets/admin-password', 'utf8'), auth_source: 'admin'};
    const target = `catalog_restore_${token}`;
    const connection = new Mongo('mongodb://127.0.0.1:27017/?directConnection=true');
    if (!connection.getDB('admin').auth(identity.username, identity.password)) throw new Error('Authentication failed');
    if (connection.getDB('admin').runCommand({listDatabases: 1, nameOnly: true}).databases.some(d => d.name === target)) throw new Error('Restore target exists');
    tool('mongorestore', [`--archive=${archive}`, '--gzip', '--nsFrom', 'retailflow.*', '--nsTo', `${target}.*`, '--stopOnError'], identity);
    print(JSON.stringify({status: 'restored_to_new_database', database: target}));
  } else { throw new Error('Unsupported action'); }
} catch (_) {
  // Tool output and exception text may contain sensitive arguments. Never forward them.
  print('Catalog backup/recovery failed; no live database promotion performed');
  quit(1);
}
