import * as Sentry from '@sentry/node';

export function captureCustomError(err: Error) {
  const client = Sentry.getCurrentHub().getClient();
  if (client) {
    client.captureException(err);
  }
}
