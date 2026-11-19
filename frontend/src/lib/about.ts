export const PROJECT_GITHUB_URL = "https://github.com/WittyRyuji/WittyCode";

export function aboutContact(value: string | undefined): { text: string; href: string } {
  const email = value?.trim() ?? "";
  // Accept one plain mailbox, not mailto parameters, display names, or lists of recipients.
  if (/^[\w+-]+(?:\.[\w+-]+)*@(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$/i.test(email)) {
    return { text: email, href: `mailto:${email}` };
  }
  return { text: "通过 GitHub Issues 联系", href: `${PROJECT_GITHUB_URL}/issues` };
}
