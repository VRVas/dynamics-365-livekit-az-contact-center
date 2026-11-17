import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const customerRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const agent = process.argv[2] === 'agent';
const root = agent ? path.resolve(customerRoot, '../agent_console') : customerRoot;
const stage = path.join(customerRoot, '.test-workspace', agent ? 'agent' : 'configured');
const appFiles = agent
  ? ['next.config.js', 'postcss.config.js', 'tailwind.config.js']
  : ['next.config.ts', 'postcss.config.mjs'];
fs.mkdirSync(stage, { recursive: true });
for (const name of ['src', 'public', 'package.json', 'package-lock.json', 'tsconfig.json', ...appFiles]) {
  fs.cpSync(path.join(root, name), path.join(stage, name), { recursive: true });
}
if (!fs.existsSync(path.join(stage, 'node_modules'))) fs.symlinkSync(path.join(root, 'node_modules'), path.join(stage, 'node_modules'), 'dir');
const configuration = JSON.parse(fs.readFileSync(path.join(stage, 'src/config/template.json'), 'utf8'));
configuration.features.agentConsole = true;
if (!agent) {
  configuration.brand = {
    name: { en: 'Configured workspace', ar: 'مساحة العمل' },
    assistantName: { en: 'Configured assistant', ar: 'مساعد الخدمة' },
    logoUrl: '/globe.svg',
    colors: { primary: '#123456', accent: '#aabbcc' },
    layout: 'voice-first',
  };
  configuration.locale = { default: 'ar', supported: ['en', 'ar'], dialingCountryCode: '+1' };
  configuration.features = { chat: true, voice: true, phone: true, profiles: true, agentConsole: true, agentAssist: false };
  configuration.welcome = {
    title: { en: 'Welcome to the workspace', ar: 'مرحباً بك' },
    description: { en: 'Choose a channel.', ar: 'اختر قناة.' },
  };
  configuration.quickActions = [{ id: 'help', label: { en: 'Get help', ar: 'طلب المساعدة' }, prompt: { en: 'Please help.', ar: 'أحتاج المساعدة.' } }];
}
fs.writeFileSync(path.join(stage, 'src/config/template.json'), JSON.stringify(configuration, null, 2));
const child = spawn(process.execPath, [
  path.join(root, 'node_modules/next/dist/bin/next'), 'dev', ...(!agent ? ['--webpack'] : []),
  '--hostname', '127.0.0.1', '--port', agent ? '38474' : '38472',
], {
  cwd: stage, env: process.env, stdio: 'inherit',
});
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
child.once('exit', (code) => {
  fs.rmSync(stage, { recursive: true, force: true, maxRetries: 3 });
  process.exit(code ?? 0);
});
