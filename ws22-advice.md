# Wavesculptor 22 advice
The Wavesculptor 22 documentation is *pretty good* but glosses over a couple of quite-important details that drive a lot of solar car teams to have some trouble. I've spent a lot of time helping teams debug a lot of problems, and I see the same few issues over and over again.

## Do not run the motor without functional thermal protection
Solar car motors are typically extremely expensive, and melting the windings (especially with a misconfigured motor controller) is shockingly easy. **Do not operate the motor without functional thermal protection**. It is simply not that hard to get a thermistor somewhere on the stator that it can do a good job of measuring the winding temperature. Do a sanity check on your readings to ensure that they are believable at a cool and warm temperature before operating the motor.

## Set Idc current limit above Sine or SixStep (phase) current limits
On the power board there are three current sensors, one each for two of the phases (the third one is assumed to be their sum) and a third on the DC bus. *On average* DC bus current will be lower than the phase current, but the sensor does not measure the average. All three sensors measure the instantaneous current and because they are Hall effect devices, they are also very noisy.

If you set the Idc limit to be the same as the sine or six step limits, inevitably while you are operating at or near the maximum phase current, noise will cause a "software over current" error or SWOC. Set the Idc limit at least ten amps above the phase current limits. There's often little reason not to set it to the maximum permissible value and using an upstream fuse that can handle that load.

## Set the minimum bus voltage above your LV bus voltage
The Wavesculptor will show something in the neighborhood of 12 volts on the DC bus even when the bus is actually off because of some quirks of its circuit design. It's also annoyingly common for teams to accidentally wire HV and LV together in part or even in full. Set the minimum bus voltage a bit above that as a way to get an error and thus better fault observability. So for example, if your LV bus is a lead-acid battery and nominally in the ~9-14 volt range, consider setting the minimum bus voltage to 20 volts.

# Don't set the maximum bus voltage to its maximum allowable value
It's tempting to think that there is no benefit to have the motor controller cease operations at some high bus voltage and to leave that to your BMS. Consider that oftentimes there is a lot of kinetic energy available for conversion back to electrical energy, and if the main contactor(s) are open, the only place for it to go is into the HV bus capacitance. And the Wavesculptor 22 has very, very little of that.

The controller is extremely slow to react to rising DC bus voltage and stop driving phase current. The lower you set the maximum bus voltage the more opportunity there is for the controller to stop switching before it brings the DC bus up over 200 volts and damages its transistors.

It's usually best to set the maximum bus voltage just below or just above your BMS trip point. Just below if you haven't gotten your BMS to do a soft shutdown, and just above if you have.

# Do not use regenerative braking with the wheel off the ground
The motor controller feeds forward the expected rotor position when calculating phase duty cycles. With the wheel off the ground, the rotor position can change very quickly due to the lack of inertia (especially if you have configured a reasonable vehicle weight). When the rotor is not where the controller expects it to be, it can erroneously draw a massive amount of current out of the motor.

Most teams' HV bus impedance is not particularly low, and a sudden and enormous current spike will often also result in a massive voltage spike, which can kill the motor controller. Do not use regenerative braking when the wheel is off the ground. If torquing the motor in free space, set a velocity limit and leave it. Fidget with the phase current target gently. Use mechanical brakes to come to a stop, or just wait.

# Add HV bus capacitance
Due to the very-low HV bus capacitance of the Wavesculptor 22 compared to many maximum power point trackers (MPPTs) in use by teams, it is very easy for motor controller dynamic currents to slosh through the HV bus into and out of the MPPT capacitors. This often causes difficult-to-diagnose problems like erratic array current readings or even blown array fuses.

Stick something between 470 and 3300 uF on the HV DC bus as close as possible to the motor controller to avoid these issues.

# BMS should carefully sequence shutdown
Soft shutdowns reduce the opportunites for hardware damage, primarily due to overvoltage driven by large changes of current through wiring inductance. If your BMS is faulting, have it enforce a zero torque target and wait for bus current to fall (or a short timeout expires) before opening any relays.

When opening relays, open the high side contactor first, the high side precharge relay second, and the low side contactor last. It's effectively the reverse of turning on the car and limits opportunities for voltage overshoot.

# Avoid EMI on your rotor position cable
**This is the single most sensitive signal on the car.** Most teams use Hall effect sensors with a relatively high pull-up resistance. It takes very little current injection to create false transitions and cause the motor controller to mis-commutate the motor, causing huge current spikes through the phase lines. If you're getting mysterious HWOC and SWOC errors, this ia a very common cause.

It's extremely tempting to bundle the motor phase lines right next to the rotor position sense lines. Don't do it. Try to route them as far apart from each other as possible, crossing orthogonally. The impact of proximity is cumulative, so any amount that you can reduce exposure is good.

If at all possible, use shielded cable for both the phase lines and for the rotor position cable. Be sure to terminate the shields to their respective returns (e.g., phase line cable shields connect to HV-, rotor position sensor cable shields connect to GND on the position sensing connector) and do not leave them floating.

# Getting good PhasorSense and ParamExtract values is tricky
The controller calibrates rotor position sensor to rotor position (PhasorSense) by reading the back EMF of the motor as you spin it and recording position sensor ticks. It needs enough voltage to get good reading resolution, but it also needs a reasonably constant rotational speed. Try to use another motor to spin the motor and have a helper push the button on the tool to capture measurements when you've gotten to a steady speed. A power drill with a 3D-printed adapter to mate to the wheel or motor works pretty well.

ParamExtract is harder. It is measuring the inductance and resistance of the motor in order to inform its feed-forward current controller model. It does this by driving a pulse of current and watching it decay. Unfortunately, that pulse of current also twitches the rotor and motion generates back EMF which causes measurement errors.

When doing a paramextract, you want to try and do it with the rotor in a position where it *does not move at all* when you perform the extraction. This is tricky, since the motor is pretty high inertia and one twitch is short, so the rotor position overshoots the "zero torque" position. I tend to add drag with my fingers and gently hold the rotor when commanding the extraction. When I don't feel any twitch, I know that the values I've captured are good. This approach may or may not be safe depending on where you put your fingers and the mechanical arrangement of your motor and anything that is attached to it.