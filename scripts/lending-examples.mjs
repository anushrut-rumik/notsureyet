import { exampleRun, fixtureClock } from '../.local/lending-build/fixtures.js';
import { evaluateRun } from '../.local/lending-build/run.js';

for (const days of [60, 30]) {
  const input = exampleRun(days);
  const result = evaluateRun(input, fixtureClock);
  console.log(JSON.stringify({
    scenario: `${days} days remaining; 800 invested tokens plus 200 separate idle tokens`,
    warning: 'ARITHMETIC FIXTURE ONLY: 1 CCIP-BnM = 1 fictional simulated dollar. No live price, fee, balance or promised earnings.',
    input,
    result,
  }, null, 2));
}
