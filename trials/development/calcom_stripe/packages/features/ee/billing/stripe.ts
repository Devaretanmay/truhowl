import Stripe from 'stripe';

const stripe = new Stripe(process.env.STRIPE_API_KEY || 'sk_test_123', {
  apiVersion: '2022-11-15',
});

export async function createBillingCharge(customerId: string, amount: number) {
  const charge = await stripe.charges.create({
    amount: amount,
    currency: 'usd',
    customer: customerId,
  });
  return charge.id;
}

export async function createCheckout(customerId: string, priceId: string) {
  const session = await stripe.checkout.sessions.create({
    payment_method_types: ['card'],
    customer: customerId,
    line_items: [{ price: priceId, quantity: 1 }],
    mode: 'subscription',
    success_url: 'https://cal.com/success',
    cancel_url: 'https://cal.com/cancel',
  });
  return session.url;
}
