class OnboardingData {
  final int? age;
  final String? incomeRange;
  final List<String> investmentGoals;
  final int? timeHorizon;
  final int? riskComfortLevel;
  final int? priorExperience;

  OnboardingData({
    this.age,
    this.incomeRange,
    required this.investmentGoals,
    this.timeHorizon,
    this.riskComfortLevel,
    this.priorExperience,
  });

  Map<String, dynamic> toJson() {
    return {
      'age': age,
      'income_range': incomeRange,
      'investment_goals': investmentGoals,
      'time_horizon': timeHorizon,
      'risk_comfort_level': riskComfortLevel,
      'prior_experience': priorExperience,
    };
  }
}

