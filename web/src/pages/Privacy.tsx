const CONTACT = "privacy@paper2proof.com";

function Contact() {
  return <a href={`mailto:${CONTACT}`}>{CONTACT}</a>;
}

export function PrivacyPage() {
  return (
    <>
      <h1>Privacy notice</h1>
      <p className="lead">
        Paper2Proof collects as little as it can: no account, no name, no email
        address, and no tracking.
      </p>
      <p className="muted">Last updated: 2 November 2026</p>

      <section aria-labelledby="privacy-who-h">
        <h2 id="privacy-who-h">Who is responsible</h2>
        <p>
          The data controller is Alessandro Tozzi (Italy), who runs Paper2Proof.
          For anything about your data, write to <Contact />.
        </p>
      </section>

      <section aria-labelledby="privacy-visit-h">
        <h2 id="privacy-visit-h">When you visit the site</h2>
        <ul>
          <li>
            Like every web server, ours receives your IP address with each
            request. We use it only to send you the page and to limit how many
            requests one address can make per minute. It is kept in the
            server&rsquo;s memory, never written to disk or to the logs, and
            dropped once it has been idle for a minute or two.
          </li>
          <li>
            No analytics, no advertising, no third-party scripts, fonts or
            embeds, and no profiling. The fonts are served from our own server.
          </li>
          <li>
            If you choose the light or dark theme, your browser remembers the
            choice in its local storage (<code>p2p-theme</code>). It never
            leaves your browser.
          </li>
        </ul>
      </section>

      <section aria-labelledby="privacy-cookie-h">
        <h2 id="privacy-cookie-h">The only cookie</h2>
        <p>
          The site sets one cookie, <code>__Host-p2p_session</code>, and only
          when you sign in with an invite code to start a run. It holds a random
          session ID and a SHA-256 fingerprint of the invite code, signed by the
          server, and expires after 7 days. It is strictly necessary for the
          service you asked for, so it needs no consent and the site shows no
          cookie banner. Browsing the gallery and the public runs sets no
          cookie.
        </p>
      </section>

      <section aria-labelledby="privacy-runs-h">
        <h2 id="privacy-runs-h">When you start a run</h2>
        <p>
          We store the arXiv ID you entered and what the run produces: the
          extracted claims, the plan, the experiment code, the measurements, the
          verdicts and the Proof Bundle. They are linked to the random session
          ID, not to you; the invite code itself is stored only as a
          fingerprint. Runs are private: one appears in the public gallery only
          if an administrator selects it.
        </p>
        <p>
          The paper&rsquo;s text and the experiment are processed by Nebius
          (model inference and isolated sandboxes). Papers are downloaded from
          arXiv, and code and packages from GitHub and PyPI. None of these
          requests carries your IP address or your session.
        </p>
      </section>

      <section aria-labelledby="privacy-where-h">
        <h2 id="privacy-where-h">Where the data is, and for how long</h2>
        <ul>
          <li>
            The site runs on a virtual server in Poland. Its database, its
            backup and the run files are kept in Nebius AI Cloud in Finland.
            Everything stays in the EU.
          </li>
          <li>
            The hosting provider and Nebius process the data only on our behalf.
          </li>
          <li>
            IP addresses: a minute or two, in memory only. The session cookie: 7
            days. Runs: while the service runs, or until you ask us to delete
            them.
          </li>
        </ul>
        <p>
          Legal basis: our legitimate interest in running and protecting the
          service (Art. 6(1)(f) GDPR) and, for the runs you start, providing the
          service you asked for (Art. 6(1)(b) GDPR).
        </p>
      </section>

      <section aria-labelledby="privacy-rights-h">
        <h2 id="privacy-rights-h">Your rights</h2>
        <p>
          You can ask to access, correct or delete your data, to restrict its
          use or to object to it, and to receive it in a portable format: write
          to <Contact />. We cannot tell which runs are yours unless you send us
          their run IDs. You can also complain to the Italian supervisory
          authority, the Garante per la protezione dei dati personali (
          <a href="https://www.garanteprivacy.it">garanteprivacy.it</a>).
        </p>
      </section>
    </>
  );
}
