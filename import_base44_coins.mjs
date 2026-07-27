#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';

const AVAILABILITY_TO_RARITY = {
  circulating: 'Circulante',
  scarce: 'Escassa',
  withdrawn: 'Retirada',
  historical: 'Histórica',
};

const COUNTRY_TO_CONTINENT = {
  Canada: 'América',
  Canadá: 'América',
  India: 'Ásia',
  Índia: 'Ásia',
};

const VALID_CONTINENTS = new Set(['Europa', 'América', 'Ásia', 'África', 'Oceânia']);
const BULK_SIZE = 100;

function parseArgs(argv) {
  const args = {
    input: '',
    country: '',
    continent: '',
    condition: 'Não Tenho',
    replace: false,
    createOnly: false,
    dryRun: true,
    limit: 0,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--input') args.input = argv[++index] || '';
    else if (arg === '--country') args.country = argv[++index] || '';
    else if (arg === '--continent') args.continent = argv[++index] || '';
    else if (arg === '--condition') args.condition = argv[++index] || '';
    else if (arg === '--replace') {
      args.replace = true;
      args.dryRun = false;
    } else if (arg === '--create-only') {
      args.createOnly = true;
      args.dryRun = false;
    } else if (arg === '--dry-run') args.dryRun = true;
    else if (arg === '--limit') args.limit = Number.parseInt(argv[++index] || '0', 10);
    else if (arg === '--help' || arg === '-h') {
      printHelp();
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  if (!args.input) throw new Error('Missing --input path to app-catalog.json');
  return args;
}

function printHelp() {
  console.log(`Usage:
  node import_base44_coins.mjs --input india/app-catalog.json --continent Ásia --dry-run
  node import_base44_coins.mjs --input india/app-catalog.json --continent Ásia --replace

Options:
  --input       Path to app-catalog.json
  --country     Override country name from the JSON
  --continent   Required if the country is not known automatically
  --condition   Default Coin.condition value (default: Não Tenho)
  --dry-run     Convert and validate locally without writing to Base44
  --replace     Delete existing Coin records for the country, then bulk-create the new records
  --create-only Create records without deleting anything first, useful for one-record tests
  --limit       Import only the first N records, useful for testing
`);
}

function loadEnvFile() {
  const envPath = path.resolve('.env');
  if (!fs.existsSync(envPath)) return;
  const text = fs.readFileSync(envPath, 'utf8');
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#') || !trimmed.includes('=')) continue;
    const separator = trimmed.indexOf('=');
    const key = trimmed.slice(0, separator).trim();
    const value = trimmed.slice(separator + 1).trim().replace(/^['"]|['"]$/g, '');
    if (!process.env[key]) process.env[key] = value;
  }
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function flattenCoins(catalogue) {
  const records = [];
  for (const period of catalogue.periods || []) {
    for (const coin of period.coins || []) {
      records.push({ period, coin });
    }
  }
  return records;
}

function buildName(coin) {
  if (coin.issuePeriod) return `${coin.denomination} (${coin.issuePeriod})`;
  return coin.denomination;
}

function toCoinRecord(entry, options, ordem) {
  const { period, coin } = entry;
  const rarity = AVAILABILITY_TO_RARITY[coin.availability];
  if (!rarity) throw new Error(`Unsupported availability value: ${coin.availability}`);
  return {
    name: buildName(coin),
    country: options.country,
    continent: options.continent,
    years: coin.issuePeriod || '',
    condition: options.condition,
    rarity,
    has_variants: false,
    image_frente: coin.obverseImage || '',
    image_verso: coin.reverseImage || '',
    notes: coin.detailUrl ? `uCoin: ${coin.detailUrl}` : period.title || '',
    ordem,
  };
}

function resolveOptions(args, catalogue) {
  const country = args.country || catalogue.country;
  if (!country) throw new Error('Country missing in JSON. Pass --country.');
  const continent = args.continent || COUNTRY_TO_CONTINENT[country];
  if (!VALID_CONTINENTS.has(continent)) {
    throw new Error(`Invalid or missing continent for ${country}. Pass --continent Europa|América|Ásia|África|Oceânia.`);
  }
  return { country, continent, condition: args.condition };
}

function chunk(records, size) {
  const chunks = [];
  for (let index = 0; index < records.length; index += size) {
    chunks.push(records.slice(index, index + size));
  }
  return chunks;
}

async function createBase44Client() {
  loadEnvFile();
  const appId = process.env.BASE44_APP_ID;
  const apiKey = process.env.BASE44_API_KEY;
  if (!appId || !apiKey) throw new Error('Missing BASE44_APP_ID or BASE44_API_KEY in environment/.env');
  const { createClient } = await import('@base44/sdk');
  return createClient({
    appId,
    headers: {
      api_key: apiKey,
    },
  });
}

async function importRecords(records, options) {
  const base44 = await createBase44Client();
  console.log(`Deleting existing Coin records for ${options.country}...`);
  await base44.entities.Coin.deleteMany({ country: options.country });
  for (const batch of chunk(records, BULK_SIZE)) {
    await base44.entities.Coin.bulkCreate(batch);
    console.log(`Created ${batch.length} records`);
  }
}

async function createRecordsOnly(records) {
  const base44 = await createBase44Client();
  for (const batch of chunk(records, BULK_SIZE)) {
    await base44.entities.Coin.bulkCreate(batch);
    console.log(`Created ${batch.length} records`);
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const catalogue = readJson(args.input);
  const options = resolveOptions(args, catalogue);
  const entries = flattenCoins(catalogue);
  const limitedEntries = args.limit > 0 ? entries.slice(0, args.limit) : entries;
  const records = limitedEntries.map((entry, index) => toCoinRecord(entry, options, index + 1));
  console.log(`Prepared ${records.length} Coin records for ${options.country} (${options.continent}).`);
  if (records[0]) console.log(JSON.stringify(records[0], null, 2));
  if (args.dryRun) {
    console.log('Dry-run only. Nothing was written to Base44. Use --create-only for a one-record test or --replace to delete and recreate records for this country.');
    return;
  }
  if (args.createOnly) {
    await createRecordsOnly(records);
  } else {
    await importRecords(records, options);
  }
  console.log('Base44 import complete.');
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});