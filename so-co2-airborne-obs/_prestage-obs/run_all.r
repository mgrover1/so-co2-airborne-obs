source('read_aircraft_10s.r')
readac10s()

source('read_aircraft_pfp.r')
readacpfp()

source('read_aircraft_med.r')
readacmed()

source('aircraft_filter_mspo_10s.r')
acfiltmspo10s()

source('aircraft_filter_mspo_pfp.r')
acfiltmspopfp()

source('aircraft_filter_mspo_med.r')
acfiltmspomed()

source('aircraft_obspack_merge.r')
acobspackmrg()

source('read_surface_obs.r')
readsurfobs()

source('read_surface_models.r') # have to run before read_aircraft_models.r to save SPO NOAA in situ record
readsurfmod() # ~30 min

source('read_aircraft_models.r')
readacmod() # ~45 min

source('read_surface_sf6.r')
readsurfsf6()

source('diur_plot_forpub.r')
