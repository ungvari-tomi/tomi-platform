// Runs only on the official image's empty-volume, loopback-only bootstrap server.
const fs = require('fs');
const catalog = db.getSiblingDB('retailflow');
const contract = JSON.parse(fs.readFileSync('/opt/catalog/contract.json', 'utf8'));
catalog.createCollection('products', {
  validator: contract.validator,
  validationLevel: contract.validationLevel,
  validationAction: contract.validationAction
});
for (const index of contract.indexes) {
  const {key, ...options} = index;
  catalog.products.createIndex(key, options);
}
catalog.createRole({role: 'catalogReader', privileges: [
  {resource: {db: 'retailflow', collection: 'products'}, actions: ['find', 'listIndexes']},
  {resource: {db: 'retailflow', collection: ''}, actions: ['listCollections']}
], roles: []});
catalog.createRole({role: 'catalogWriter', privileges: [
  {resource: {db: 'retailflow', collection: 'products'}, actions: ['find', 'insert', 'update']}
], roles: []});
for (const [name, role] of [['reader', 'catalogReader'], ['writer', 'catalogWriter']]) {
  const secret = JSON.parse(fs.readFileSync(`/run/secrets/${name}.json`, 'utf8'));
  catalog.createUser({user: secret.username, pwd: secret.password, roles: [role]});
}
// No documents or credential values are printed. Python seeds through the writer identity.
