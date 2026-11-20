# PCS Scanner: privacy policy

_Last updated: 19 November 2026_

PCS Scanner is a free Windows app used by the players of the PCS Apex Legends team to record their
games, scan the recordings for coaching, and share the results with their coach. It is not sold and
has no ads.

## What the app does with your Google account

You sign in with Google only if you want to use these features, and you can sign out at any time by
removing PCS Scanner from <https://myaccount.google.com/permissions>.

- **Google Drive**: not used any more. Older versions could put scan results in a "PCS Scans" folder in
  your Drive; this version never opens Drive. You may delete that folder yourself (the app never deletes it).
- **YouTube** (`youtube.upload`): if you turn on YouTube upload, the app uploads the videos of your scrim
  blocks to the YouTube channel you pick when signing in, with a title and description. It cannot read,
  change or delete any video.
- **Who you are** (`openid`, `email`): the same Google sign-in tells the app your Google account id and email
  address. If you are on your team's hub, the app sends them there once, so your coach sees which Google account
  each hub key belongs to. Only your coach sees it; other players don't. The app keeps no copy of your email and
  never writes it to its log. It is not used for anything else.

## What goes to your team's hub

The hub is your team's own server, run by your coach (not by Google or anyone else). If you joined it, the app
sends it, with your hub key: your Google account (above), the scan results (small text files), your microphone track if you said yes to "Send
my mic track" (only after the voice consent below), crash reports (below) and your sharing choices. Videos never
go to the hub: they go only to YouTube, if you turn that on. Your coach and your teammates on the same hub can see
your scan results; players of another team cannot.

## What stays on your PC

Recordings, settings, sign-in tokens and logs stay in your Windows user folder
(`%LOCALAPPDATA%\PCS Scanner` and the recordings folder you choose). Recordings are deleted
automatically according to the limits you set (number of days and maximum folder size).

Crash reports: if you are on your team's hub, when recording, OBS or a scan fails the app sends a short report
to that hub only (error, app and Windows version, graphics card, end of the app's log, with keys, passwords,
e-mail addresses and your Windows user name removed), for your coach. Turn it off in Settings → Team hub. Nothing
goes to any other service.

Voice (Discord and microphone) is only recorded after you confirm in the app that your team agreed to
it. Only the tracks you choose are sent, and only to your team's hub: never to Google Drive.

If you choose "Share mic tracks with my team" (first-run setup or Settings), your microphone track is
shared with your teammates on the team's server, and the app downloads your teammates' shared microphone
tracks of the games you played into its own folder (`%LOCALAPPDATA%\PCS Scanner\team-tracks`), deleted
with your recording. Players of another team never get them. You can turn it off at any time in Settings;
your teammates then no longer get your track. Comms transcripts are not made yet; the app only keeps your
answer (off until you say yes).

## What is never done

- No data is sold, rented or given to anyone outside your team.
- No advertising, analytics or tracking.
- Data received from Google APIs is used only for the features above, in line with the
  [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
  including its Limited Use requirements.

## Contact

Questions or deletion requests: the support email shown on PCS Scanner's Google sign-in screen, or your
team's coach. Source code: <https://github.com/BLAKYY5/pcs-scanner>.
