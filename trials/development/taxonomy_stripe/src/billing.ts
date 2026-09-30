import Stripe from 'stripe';

const stripe = new Stripe(process.env.STRIPE_API_KEY || '');

export async function processCharge(amount: number) {
  return await stripe.charges.create({
    amount: amount,
    currency: 'usd',
  });
}
