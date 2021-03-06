components/components/
----------------------

Modules in this directory are shared between all AppEngine servers in this
repository.


### Contents of this directory:

  - [auth/](auth) is a library that provides authorization and authentication
    functionality for webapp2 and Cloud Endpoints app. Acts as a client for
    auth_service.
  - [config/](config) is a client for config_service that provides API for
    fetching configs.
  - [datastore_utils/](datastore_utils) is utility code to enhance NDB.
  - [ereporter2/](ereporter2) is a standalone components that sends alerts based
    on reading the server's log.
  - [metrics/](metrics) is a module to interact with [Cloud Monitoring
    Metrics](https://cloud.google.com/monitoring/custom-metrics).
  - [static/](static) contains third party javascript libraries.
  - [third_party/](third_party) contains third party python libraries used by
    components.


### Using components/

  1. Symlink components/ into your application, e.g.:
     `ln -s ../components/components`
