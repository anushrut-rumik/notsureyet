import test from 'node:test';
import assert from 'node:assert/strict';
import {planDurationSeconds} from '../scripts/part4/rules.mjs';

test('hour and minute durations are exact; legacy days and defaults still work',()=>{
 assert.equal(planDurationSeconds({durationSeconds:7200}),7200n);
 assert.equal(planDurationSeconds({durationSeconds:90*60}),5400n);
 assert.equal(planDurationSeconds({days:60}),60n*86400n);
 assert.equal(planDurationSeconds({}),30n*86400n);
});
test('conflicting, negative, fractional or coerced durations cannot change an approval window',()=>{
 for(const settings of [{days:1,durationSeconds:7200},{durationSeconds:0},{durationSeconds:-7200},
  {durationSeconds:7200.5},{durationSeconds:'7200'},{durationSeconds:true},{days:1.5}]){
  assert.throws(()=>planDurationSeconds(settings));
 }
});
