const fs = require('fs');
try {
  const secret = JSON.parse(fs.readFileSync('/run/secrets/reader.json', 'utf8'));
  const connection = new Mongo('mongodb://127.0.0.1:27017/?directConnection=true');
  const catalog = connection.getDB('retailflow');
  if (!catalog.auth(secret.username, secret.password)) quit(1);
  catalog.products.find({}, {_id: 1}).limit(1).toArray();
  quit(0);
} catch (_) { quit(1); }
