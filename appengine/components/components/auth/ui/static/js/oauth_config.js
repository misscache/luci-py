// Copyright 2014 The Swarming Authors. All rights reserved.
// Use of this source code is governed by the Apache v2.0 license that can be
// found in the LICENSE file.

var oauth_config = (function() {
'use strict';

var exports = {};
var form = null;


// Message shown when config is successfully updated.
var UPDATED_TEXT = 'The change takes up to 1 min to propagate to all servers.';


// Called when HTML body of a page is loaded.
exports.onContentLoaded = function() {
  var $form = $('#oauth-config-form');
  var $alerts = $('#oauth-config-alerts');

  // Form submit handler.
  $form.submit(function(event) {
    event.preventDefault();

    // Grab data from the form.
    var id = $('input[name="client_id"]', $form).val();
    var secret = $('input[name="client_secret"]', $form).val();
    var more_ids = $('textarea[name="more_ids"]', $form).val().split('\n');

    // Disable UI while request is in flight.
    common.setInteractionDisabled($form, true);

    // Show alert box with operation result, enable back UI.
    var showResult = function(type, title, message) {
      $alerts.html(common.getAlertBoxHtml(type, title, message));
      common.setInteractionDisabled($form, false);
    };

    // Push it to the server.
    api.updateOAuthConfig(id, secret, more_ids).then(function(response) {
      showResult('success', 'Config updated.', UPDATED_TEXT);
    }, function(error) {
      showResult('error', 'Oh snap!', error.text);
    });
  });

  // Fetch the config, then show it.
  api.fetchOAuthConfig().then(function(response) {
    var config = response.data;
    $('input[name="client_id"]', $form).val(config.client_id);
    $('input[name="client_secret"]', $form).val(config.client_not_so_secret);
    $('textarea[name="more_ids"]', $form).val(
        (config.additional_client_ids || []).join('\n'));
    common.presentContent();
  }, function(error) {
    common.presentError(error.text);
  });
};


return exports;
}());
