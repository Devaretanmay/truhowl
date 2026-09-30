import octokit from '@octokit/rest';

export function getClient() {
  return new octokit();
}
