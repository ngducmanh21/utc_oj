(function () {
    'use strict';

    function init(input, error) {
        var domain = input.dataset.registrationEmailDomain;
        var message = input.dataset.registrationEmailError;

        function validate() {
            var value = input.value.trim();
            var emailDomain = value.slice(value.lastIndexOf('@') + 1).toLowerCase();
            input.setCustomValidity(value && !input.validity.typeMismatch && emailDomain !== domain ? message : '');
            error.textContent = input.validationMessage;
            error.hidden = !error.textContent;
            input.setAttribute('aria-invalid', input.validity.valid ? 'false' : 'true');
        }

        input.addEventListener('input', validate);
        input.addEventListener('change', validate);
        input.addEventListener('blur', function () {
            input.value = input.value.trim();
            validate();
        });
        input.form.addEventListener('submit', function (event) {
            input.value = input.value.trim();
            validate();
            if (!input.checkValidity()) {
                event.preventDefault();
                input.reportValidity();
            }
        });
    }

    function setup() {
        var input = document.querySelector('[data-registration-email-domain]');
        if (input) {
            init(input, document.getElementById('registration-email-error'));
        }
    }

    window.RegistrationEmail = {init: init};
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', setup);
    } else {
        setup();
    }
}());
