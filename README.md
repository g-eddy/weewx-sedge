# weewx-sedge
<p>SolarEdge provide an inverter for solar power generation.<br />
this service retrieves solar energy readings that
the vendor has uploaded to its cloud (see www.solaredge.com)</p>

<p>weewx.conf configuration:</p>
<pre>
  [SolarEdge]
      api_key     your api key from_vendor (no default)
      site_id     your site id from installer (no default)
      data_type   data_type to be inserted (default: solarEnergy)
      poll_interval polling interval /secs (default: 15 mins, must be at
                  least this long due to api limitations)
      binding     one or more of 'loop', 'archive' (default: archive)
</pre>
<p>example weewx.conf entry:</p>
<pre>
  [SolarEdge]
      api_key = your_api_key_from_vendor
      site_id = your_site_id_from_installer
      #data_type = solarEnergy
      #poll_interval = 900
      #binding = archive
 </pre>
