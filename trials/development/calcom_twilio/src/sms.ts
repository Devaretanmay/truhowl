import axios from 'axios';

export async function sendNotification(body: string) {
  return axios.post('https://api.ashburn.twilio.com/2010-04-01/Accounts/AC123/Messages.json', {
    Body: body,
  });
}
