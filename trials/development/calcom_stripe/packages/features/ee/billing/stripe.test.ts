import { describe, it, expect } from 'vitest';
import { createBillingCharge } from './stripe';

describe('Billing', () => {
  it('defines createBillingCharge function', () => {
    expect(typeof createBillingCharge).toBe('function');
  });
});
